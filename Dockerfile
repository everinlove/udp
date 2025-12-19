# --- 第一阶段：构建与编译 ---
FROM python:3.9-slim-bullseye as builder

WORKDIR /build

# 安装编译所需的系统依赖 (GCC等)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY core.py setup.py ./

# 执行编译：生成 .so 文件
# build_ext --inplace 会在当前目录生成 .so 文件
RUN python setup.py build_ext --inplace

# --- 第二阶段：最终镜像 ---
FROM python:3.9-slim-bullseye

WORKDIR /app

# 安装运行时依赖 (不需要 Cython 和 GCC 了)
# 我们只安装 Flask 和 Requests
RUN pip install --no-cache-dir flask requests

# 从 builder 阶段复制编译好的二进制模块 (.so)
# 注意：文件名通常包含架构信息，如 core.cpython-39-aarch64-linux-gnu.so
# 但 Python import 时会自动识别，只要在 PYTHONPATH 下即可
COPY --from=builder /build/*.so ./

# 复制入口文件 (这个是明文，但只有一行 import)
COPY app.py .

# 清理不必要的文件 (保险起见)
RUN rm -f *.pyc *.c

EXPOSE 5000

CMD ["python", "app.py"]
