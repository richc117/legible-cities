# Slim, architecture-native LOOM build.
#
# The upstream Dockerfile pins ubuntu:20.04 and downloads Gurobi's linux64
# tarball, which forces amd64 emulation on Apple Silicon. LOOM works fine with
# the open ILP solvers (COIN-CBC, GLPK), so we drop Gurobi and build natively.
FROM ubuntu:24.04 AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install --no-install-recommends -y \
      ca-certificates git make cmake g++ \
      libglpk-dev glpk-utils \
      coinor-libcbc-dev coinor-cbc \
      libprotobuf-dev protobuf-compiler \
      libzip-dev \
    && rm -rf /var/lib/apt/lists/*

ARG LOOM_REF=master
RUN git clone --recurse-submodules https://github.com/ad-freiburg/loom /loom \
    && cd /loom && git checkout ${LOOM_REF} && git submodule update --init --recursive \
    && mkdir build && cd build && cmake .. && make -j"$(nproc)" install

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# The -dev packages pull in the matching runtime shared libraries. Naming the
# runtime sonames directly is version-fragile across Ubuntu releases and buys
# only a couple hundred MB we do not care about here.
RUN apt-get update && apt-get install --no-install-recommends -y \
      libglpk-dev coinor-libcbc-dev libprotobuf-dev libzip-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/bin/ /usr/local/bin/
WORKDIR /work
CMD ["loom", "--help"]
