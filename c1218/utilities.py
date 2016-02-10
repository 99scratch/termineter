#  c1218/utilities.py
#
#  Copyright 2011 Spencer J. McIntyre <SMcIntyre [at] SecureState [dot] net>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

import struct

import CrcMoose  # Get it from: http://www.nightmare.com/~ryb/code/CrcMoose.py

crc = CrcMoose.CRC_HDLC.calcString
crc_str = lambda x: struct.pack('<H', crc(x))

def data_checksum(data):
	chksum = 0
	for i in struct.unpack('B' * len(data), data):
		chksum += i
	chksum = ((chksum - 1) & 0xff) ^ 0xff
	return struct.pack('B', chksum)
