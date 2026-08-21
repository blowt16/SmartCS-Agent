# pgvector/pgvector:pg16 + pg_jieba（中文全文检索分词扩展）
# pg_jieba 基于 cppjieba，与应用侧 jieba 同源算法，分词行为一致
#
# 依赖获取策略（vendor 化，构建期零外网下载）：
#   容器内访问 gitee/GitHub/代理均不稳定（已实测失败），
#   改为宿主机预下载到 vendor/（见 vendor/README.md 的获取命令），
#   构建时 COPY 进镜像，仅 apt 使用清华镜像。
FROM pgvector/pgvector:pg16

# debian 官方源换阿里云镜像（仅构建期加速；清华镜像对部分 deb 文件 403 已实测）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

ENV PATH="/usr/lib/postgresql/16/bin:${PATH}"

# 层 1：构建工具（独立缓存层，仅此层变化时才重跑 apt）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates gcc g++ make cmake unzip postgresql-server-dev-16 libpq-dev

# 层 2：vendor 依赖（宿主机下载，见 vendor/README.md）
COPY vendor/pg_jieba.zip /tmp/pg_jieba.zip
COPY vendor/cppjieba.tar.gz /tmp/cppjieba.tar.gz

# 层 3：解压源码 + 补齐 libjieba + CMake 编译安装 + 部署词典 + 清理
RUN cd /tmp && unzip -q pg_jieba.zip && mv pg_jieba-master /tmp/pg_jieba \
    && tar -xzf /tmp/cppjieba.tar.gz -C /tmp \
    && mkdir -p /tmp/pg_jieba/libjieba/deps /tmp/pg_jieba/libjieba/include \
    && cp -r /tmp/cppjieba-0.1.1/deps/limonp /tmp/pg_jieba/libjieba/deps/ \
    && cp -r /tmp/cppjieba-0.1.1/include/cppjieba /tmp/pg_jieba/libjieba/include/ \
    && mkdir -p /tmp/pg_jieba/libjieba/dict \
    && cp -r /tmp/cppjieba-0.1.1/python/cppjieba_module/cppjieba/dict/* /tmp/pg_jieba/libjieba/dict/ \
    # CMakeLists 的 include 相对路径在 CMake 3.25+ 报错，改绝对路径
    && sed -i 's|INCLUDE_DIRECTORIES(${CPPJIEBA_DIR}|INCLUDE_DIRECTORIES(${CMAKE_CURRENT_SOURCE_DIR}/${CPPJIEBA_DIR}|g' /tmp/pg_jieba/CMakeLists.txt \
    # find_package(PostgreSQL) 在 Debian 布局下找不到 server 头目录，显式传参
    && cmake -S /tmp/pg_jieba -B /tmp/pg_jieba/build \
        -DPostgreSQL_INCLUDE_DIR="$(pg_config --includedir)" \
        -DPostgreSQL_TYPE_INCLUDE_DIR="$(pg_config --includedir-server)" \
        -DPostgreSQL_LIBRARY="$(pg_config --libdir)/libpq.so" \
    && cmake --build /tmp/pg_jieba/build -j"$(nproc)" \
    && cmake --install /tmp/pg_jieba/build \
    && cp /tmp/cppjieba-0.1.1/python/cppjieba_module/cppjieba/dict/jieba.dict.utf8 \
        "$(pg_config --sharedir)/tsearch_data/jieba_base.utf8" \
    && cp /tmp/cppjieba-0.1.1/python/cppjieba_module/cppjieba/dict/hmm_model.utf8 \
        "$(pg_config --sharedir)/tsearch_data/jieba_hmm.utf8" \
    && rm -rf /tmp/pg_jieba /tmp/pg_jieba.zip /tmp/cppjieba*
