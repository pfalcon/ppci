
from .cmo import read_file


__all__ = ['read_file']


if __name__ == '__main__':
    import sys
    read_file(sys.argv[0])