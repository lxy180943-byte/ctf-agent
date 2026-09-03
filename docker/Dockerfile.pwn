FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="ctf-agent pwn"
LABEL org.opencontainers.image.description="Binary exploitation triage tools for authorized CTF targets."

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 \
      python3-pip \
      python3-dev \
      python3-pwntools \
      python3-ropgadget \
      build-essential \
      git \
      file \
      binutils \
      gdb \
      gdbserver \
      checksec \
      ruby \
      bash \
      coreutils \
    && mkdir -p /opt/ctf-agent \
    && printf "%s\n" \
      "pwntools and ROPgadget are installed from Ubuntu packages for repeatable competition builds." \
      "If a challenge needs newer PyPI versions, use a derived image or temporary container." \
      "one_gadget is intentionally not installed by default." \
      "Install it only when needed: gem install one_gadget" \
      > /opt/ctf-agent/README.pwn-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
CMD ["bash"]
