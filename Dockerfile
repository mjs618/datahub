# 多架构构建说明：
#   基础镜像 python:3.9-slim 官方已提供 linux/amd64、linux/arm64、linux/arm/v7
#   当前依赖 (asyncua、requests) 均为纯 Python，无需架构相关编译工具链
#   构建命令：
#     docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 \
#       -t <registry>/data-hub:latest --push .
#
# 使用 DaoCloud 备用镜像源
FROM docker.m.daocloud.io/library/python:3.9-slim

# buildx 自动注入的目标架构变量（当前依赖纯 Python 无需使用，预留便于后续扩展）
ARG TARGETARCH
ARG TARGETVARIANT

# Set the working directory in the container
WORKDIR /app

# Install curl for an alternative healthcheck option (kept minimal)
# RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# 配置国内 Debian 镜像源（阿里云）以提高下载速度和成功率
RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# Install build dependencies for cffi (needed by asyncua)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Create data directory for persistent state
RUN mkdir -p /data

# Run main.py when the container launches
CMD ["python", "main.py"]
