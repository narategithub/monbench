#!/bin/bash

cd $(dirname $0)

ARCH=${ARCh:-$(uname -m | sed -e 's/aarch64/arm64/' -e 's/x86_64/amd64/')}

set -x
docker build -t ovishpc/monbench:main-${ARCH} .
