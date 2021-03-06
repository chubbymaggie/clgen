#!/usr/bin/env bash
#
# One-liner to install CLgen 0.2.5.
#
# Copyright 2016, 2017 Chris Cummins <chrisc.101@gmail.com>.
#
# This file is part of CLgen.
#
# CLgen is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CLgen is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CLgen.  If not, see <http://www.gnu.org/licenses/>.
#
set -ex
wget https://github.com/ChrisCummins/clgen/archive/0.2.5.tar.gz -O clgen-0.2.5.tar.gz
tar xf clgen-0.2.5.tar.gz
rm clgen-0.2.5.tar.gz
cd clgen-0.2.5
./configure --batch --with-opencl
make

if [[ -n "$VIRTUAL_ENV" ]]; then
    # virtualen - no sudo required
    make install
    make test
    cd ..
    rm -rf clgen-0.2.5
else
    # system-wide - use sudo
    sudo -H make install
    sudo -H make test
    cd ..
    sudo rm -rf clgen-0.2.5
fi
echo "==> CLgen 0.2.5 installed"
