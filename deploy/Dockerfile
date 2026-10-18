# 单镜像双角色：同一份代码，靠 command 决定跑采集还是跑接口。
#
#     docker build -t collector-platform -f deploy/Dockerfile .
#     docker run --rm -v "$PWD/data:/app/data" collector-platform            # 采集
#     docker run --rm -p 8000:8000 -v "$PWD/data:/app/data" collector-platform \
#         .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000          # 接口
#
# 之所以不写成两个镜像：两者的依赖完全相同，拆开只会让「改了一行代码要重建两次」。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COLLECTOR_CONFIG=/app/config/app.json

WORKDIR /app

# 先只拷依赖清单：只要 requirements.txt 没变，这一层就能命中缓存，
# 改代码时不用重装一遍依赖。
COPY requirements.txt ./
RUN python -m venv .venv \
    && .venv/bin/pip install --upgrade pip \
    && .venv/bin/pip install -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/
COPY sql/ ./sql/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

# 数据、日志、报告都是运行期产物，挂在卷上才能持久化。
RUN mkdir -p data/replay logs reports \
    && useradd --create-home --uid 10001 collector \
    && chown -R collector:collector /app
USER collector

# 采集进程需要连 PLC（502）与仪表，这里只声明用途，不暴露端口。
CMD [".venv/bin/python", "scripts/run_collector.py", "--config", "config/app.json"]
