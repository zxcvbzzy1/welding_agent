"""原 CADTpath 脚本的命令行兼容入口，算法已迁移至 cad_path.py。"""

if __package__:
    from .cad_path import main
else:
    from cad_path import main


if __name__ == "__main__":
    main()
