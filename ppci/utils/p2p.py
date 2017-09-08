""" Python to ppci conversion.

"""

import logging
import ast
from .. import ir, irutils, api
from ..common import SourceLocation, CompilerError
from ..binutils import debuginfo


def load_py(f, functions=None):
    """ Load a type annotated python file.

    arguments:
    f: a file like object containing the python source code.
    """
    from ..import api
    from .codepage import load_obj

    logging.basicConfig(level=logging.DEBUG)
    debug_db = debuginfo.DebugDb()
    mod = P2P(debug_db).compile(f)
    # txt = io.StringIO()
    # writer = irutils.Writer(txt)
    # writer.write(mod)
    # print(txt.getvalue())
    arch = api.get_current_platform()
    obj = api.ir_to_object([mod], arch, debug_db=debug_db, debug=True)
    m2 = load_obj(obj)
    return m2


def py_to_ir(f):
    """ Compile a piece of python code to an ir module.

    Args:
        f (file-like-object): a file like object containing the python code

    Returns:
        A :class:`ppci.ir.Module` module
    """
    debug_db = debuginfo.DebugDb()
    mod = P2P(debug_db).compile(f)
    return mod


class Var:
    def __init__(self, value, lvalue, ty):
        self.value = value
        self.lvalue = lvalue
        self.ty = ty


class P2P:
    """ Not peer-to-peer but python to ppci :) """
    logger = logging.getLogger('p2p')

    def __init__(self, debug_db):
        self.debug_db = debug_db

    def compile(self, f):
        """ Convert python into IR-code.

        Arguments:
            f: the with the python code

        Returns:
            the ir-module.
        """
        src = f.read()
        self._filename = getattr(f, 'name', None)
        # Parse python code:
        x = ast.parse(src)

        self.builder = irutils.Builder()
        self.builder.prepare()
        self.builder.set_module(ir.Module('foo'))
        for df in x.body:
            self.logger.debug('Processing %s', df)
            if isinstance(df, ast.FunctionDef):
                self.gen_function(df)
            else:
                raise NotImplementedError('Cannot do!'.format(df))
        mod = self.builder.module
        irutils.Verifier().verify(mod)
        return mod

    def emit(self, i):
        self.builder.emit(i)
        return i

    def get_ty(self, annotation):
        # TODO: assert isinstance(annotation, ast.Annotation)
        type_mapping = {
            'int': ir.i64,
            'float': ir.f64,
        }
        type_name = annotation.id
        if type_name in type_mapping:
            return type_mapping[type_name]
        else:
            raise Exception('Need to return int')

    def get_variable(self, name):
        if name not in self.local_map:
            # Create a variable with the given name
            # TODO: for now i64 is assumed to be the only type!
            mem = self.emit(ir.Alloc('alloc_{}'.format(name), 8))
            self.local_map[name] = Var(mem, True, ir.i64)
        return self.local_map[name]

    def gen_function(self, df):
        self.local_map = {}

        dbg_int = debuginfo.DebugBaseType('int', 8, 1)
        ir_function = self.builder.new_function(df.name, self.get_ty(df.returns))
        dbg_args = []
        for arg in df.args.args:
            if not arg.annotation:
                raise Exception('Need type annotation for {}'.format(arg.arg))
            aty = self.get_ty(arg.annotation)
            name = arg.arg

            # Debug info:
            param = ir.Parameter(name, aty)
            dbg_args.append(debuginfo.DebugParameter(name, dbg_int))

            ir_function.add_parameter(param)
        self.logger.debug('Created function %s', ir_function)
        self.builder.block_number = 0
        self.builder.set_function(ir_function)

        dfi = debuginfo.DebugFunction(
            ir_function.name,
            SourceLocation('foo.py', 1, 1, 1),
            dbg_int,
            dbg_args)
        self.debug_db.enter(ir_function, dfi)

        first_block = self.builder.new_block()
        self.builder.set_block(first_block)
        ir_function.entry = first_block

        # Copy the parameters to variables (so they can be modified):
        for parameter in ir_function.arguments:
            # self.local_map[name] = Var(param, False, aty)
            para_var = self.get_variable(parameter.name)
            self.emit(ir.Store(parameter, para_var.value))

        self.block_stack = []
        self.gen_statement(df.body)
        assert not self.block_stack

        # TODO: ugly:
        ir_function.delete_unreachable()

    def gen_statement(self, statement):
        """ Generate code for a statement """
        if isinstance(statement, list):
            for inner_statement in statement:
                self.gen_statement(inner_statement)
        elif isinstance(statement, ast.Pass):
            pass  # No comments :)
        elif isinstance(statement, ast.Return):
            value = self.gen_expr(statement.value)
            self.emit(ir.Return(value))
            void_block = self.builder.new_block()
            self.builder.set_block(void_block)
        elif isinstance(statement, ast.If):
            ja_block = self.builder.new_block()
            else_block = self.builder.new_block()
            continue_block = self.builder.new_block()
            self.gen_cond(statement.test, ja_block, else_block)

            # Yes
            self.builder.set_block(ja_block)
            self.gen_statement(statement.body)
            self.emit(ir.Jump(continue_block))

            # Else:
            self.builder.set_block(else_block)
            self.gen_statement(statement.orelse)
            self.emit(ir.Jump(continue_block))

            self.builder.set_block(continue_block)
        elif isinstance(statement, ast.While):
            if statement.orelse:
                self.error(statement, 'while-else not supported')
            test_block = self.builder.new_block()
            body_block = self.builder.new_block()
            continue_block = self.builder.new_block()

            # Test:
            self.emit(ir.Jump(test_block))
            self.builder.set_block(test_block)
            self.gen_cond(statement.test, body_block, continue_block)

            # Body:
            self.block_stack.append(continue_block)
            self.builder.set_block(body_block)
            self.gen_statement(statement.body)
            self.emit(ir.Jump(test_block))
            self.block_stack.pop()

            # The end:
            self.builder.set_block(continue_block)
        elif isinstance(statement, ast.Break):
            unreachable_block = self.builder.new_block()
            break_block = self.block_stack[-1]
            self.emit(ir.Jump(break_block))
            self.builder.set_block(unreachable_block)
        elif isinstance(statement, ast.For):
            # Check else-clause:
            if statement.orelse:
                self.error(statement, 'for-else not supported')

            # Allow for loop with range in it:
            if not isinstance(statement.iter, ast.Call):
                self.error(statement.iter, 'Only range supported in for loops')

            if statement.iter.func.id != 'range':
                self.error(statement.iter, 'Only range supported in for loops')

            # Determine start and end values:
            ra = statement.iter.args
            if len(ra) == 1:
                i_init = self.emit(ir.Const(0, 'i_init', ir.i64))
                n2 = self.gen_expr(ra[0])
            elif len(ra) == 2:
                i_init = self.gen_expr(ra[0])
                n2 = self.gen_expr(ra[1])
            else:
                self.error(statement.iter, 'Does not support {} arguments'.format(len(ra)))

            entry_block = self.builder.block
            test_block = self.builder.new_block()
            body_block = self.builder.new_block()
            continue_block = self.builder.new_block()

            self.emit(ir.Jump(test_block))

            # Test block:
            self.builder.set_block(test_block)
            i_phi = self.emit(ir.Phi('i_phi', ir.i64))
            i_phi.set_incoming(entry_block, i_init)
            self.emit(ir.CJump(i_phi, '<', n2, body_block, continue_block))

            # Publish looping variable:
            self.local_map[statement.target.id] = Var(i_phi, False, ir.i64)

            # Body:
            self.block_stack.append(continue_block)
            self.builder.set_block(body_block)
            self.gen_statement(statement.body)
            self.block_stack.pop()

            # Increment loop variable:
            one = self.emit(ir.Const(1, 'one', ir.i64))
            i_inc = self.emit(ir.add(i_phi, one, 'i_inc', ir.i64))
            i_phi.set_incoming(body_block, i_inc)

            # Jump to start again:
            self.emit(ir.Jump(test_block))

            # The end:
            self.builder.set_block(continue_block)
        elif isinstance(statement, ast.Assign):
            assert len(statement.targets) == 1
            name = statement.targets[0].id
            var = self.get_variable(name)
            assert var.lvalue
            value = self.gen_expr(statement.value)
            self.emit(ir.Store(value, var.value))
        elif isinstance(statement, ast.Expr):
            self.gen_expr(statement.value)
        elif isinstance(statement, ast.AugAssign):
            name = statement.target.id
            assert isinstance(name, str)
            var = self.get_variable(name)
            assert var.lvalue
            lhs = self.emit(ir.Load(var.value, 'load', var.ty))
            rhs = self.gen_expr(statement.value)
            op = self.binop_map[type(statement.op)]
            value = self.emit(ir.Binop(lhs, op, rhs, 'augassign', var.ty))
            self.emit(ir.Store(value, var.value))
        else:  # pragma: no cover
            self.not_impl(statement)

    def gen_cond(self, c, yes_block, no_block):
        if isinstance(c, ast.Compare):
            # print(dir(c), c.ops, c.comparators)
            assert len(c.ops) == len(c.comparators)
            assert len(c.ops) == 1
            op_map = {
                ast.Gt: '>', ast.Lt: '<',
                ast.Eq: '=', ast.NotEq: '!=',
            }

            a = self.gen_expr(c.left)
            op = op_map[type(c.ops[0])]
            b = self.gen_expr(c.comparators[0])
            self.emit(ir.CJump(a, op, b, yes_block, no_block))
        else:  # pragma: no cover
            self.not_impl(c)
        
    binop_map = {
        ast.Add: '+', ast.Sub: '-',
        ast.Mult: '*', ast.Div: '/',
    }

    def gen_expr(self, expr):
        """ Generate code for a single expression """
        if isinstance(expr, ast.BinOp):
            a = self.gen_expr(expr.left)
            b = self.gen_expr(expr.right)
            op = self.binop_map[type(expr.op)]
            v = self.emit(ir.Binop(a, op, b, 'add', ir.i64))
            return v
        elif isinstance(expr, ast.Name):
            var = self.local_map[expr.id]
            if var.lvalue:
                value = self.emit(ir.Load(var.value, 'load', ir.i64))
            else:
                value = var.value
            return value
        elif isinstance(expr, ast.Num):
            return self.emit(ir.Const(expr.n, 'num', ir.i64))
        else:  # pragma: no cover
            self.not_impl(expr)

    def not_impl(self, node):
        print(dir(node))
        self.error(node, 'Cannot do {}'.format(node))

    def error(self, node, message):
        """ Raise a nice error message as feedback """
        location = SourceLocation(self._filename, node.lineno, node.col_offset + 1, 1)
        raise CompilerError(message, location)
