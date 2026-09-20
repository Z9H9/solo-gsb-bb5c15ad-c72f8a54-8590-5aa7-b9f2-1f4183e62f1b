# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖安装纳入镜像构建
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app ./app

# 非 root 运行
RUN useradd --system --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# 启动初始化纳入镜像：容器启动即运行服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
