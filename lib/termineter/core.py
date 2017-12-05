#  termineter/core.py
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

from __future__ import unicode_literals

import binascii
import importlib
import logging
import logging.handlers
import os
import re
import serial
import sys

import c1218.connection
import c1218.errors
import termineter.module
import termineter.errors
import termineter.options
import termineter.utilities

import serial.serialutil
import smoke_zephyr.utilities

class Framework(object):
	"""
	This is the main instance of the framework.  It contains and
	manages the serial connection as well as all of the loaded
	modules.
	"""
	def __init__(self, stdout=None):
		self.__package__ = '.'.join(self.__module__.split('.')[:-1])
		package_path = importlib.import_module(self.__package__).__path__[0]  # that's some python black magic trickery for you
		if stdout is None:
			stdout = sys.stdout
		self.stdout = stdout
		self.logger = logging.getLogger('termineter.framework')

		self.directories = termineter.utilities.Namespace()
		self.directories.user_data = os.path.abspath(os.path.join(os.path.expanduser('~'), '.termineter'))
		self.directories.data_path = os.path.abspath(os.path.join(package_path, 'data'))
		if not os.path.isdir(self.directories.data_path):
			self.logger.critical('path to data not found')
			raise termineter.errors.FrameworkConfigurationError('path to data not found')
		if not os.path.isdir(self.directories.user_data):
			os.mkdir(self.directories.user_data)

		self.serial_connection = None
		self._serial_connected = False

		# setup logging stuff
		main_file_handler = logging.handlers.RotatingFileHandler(os.path.join(self.directories.user_data, self.__package__ + '.log'), maxBytes=262144, backupCount=5)
		main_file_handler.setLevel(logging.DEBUG)
		main_file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)-50s %(levelname)-10s %(message)s"))
		logging.getLogger('').addHandler(main_file_handler)

		# setup and configure options
		# Whether or not these are 'required' is really enforced by the individual
		# modules get_missing_options method and by which options they require based
		# on their respective types.  See framework/templates.py for more info.
		self.options = termineter.options.Options(self.directories)
		self.options.add_boolean('USECOLOR', 'enable color on the console interface', default=False)
		self.options.add_string('CONNECTION', 'serial connection string')
		self.options.add_string('USERNAME', 'serial username', default='0000')
		self.options.add_integer('USERID', 'serial userid', default=0)
		self.options.add_string('PASSWORD', 'serial c12.18 password', default='00000000000000000000')
		self.options.add_boolean('PASSWORDHEX', 'if the password is in hex', default=True)
		self.advanced_options = termineter.options.AdvancedOptions(self.directories)
		self.advanced_options.add_boolean('AUTOCONNECT', 'automatically handle connections for modules', default=True)
		self.advanced_options.add_integer('BAUDRATE', 'serial connection baud rate', default=9600)
		self.advanced_options.add_integer('BYTESIZE', 'serial connection byte size', default=serial.EIGHTBITS)
		self.advanced_options.add_boolean('CACHETBLS', 'cache certain read-only tables', default=True)
		self.advanced_options.set_callback('CACHETBLS', self._opt_callback_set_table_cache_policy)
		self.advanced_options.add_integer('STOPBITS', 'serial connection stop bits', default=serial.STOPBITS_ONE)
		self.advanced_options.add_integer('NBRPKTS', 'c12.18 maximum packets for reassembly', default=2)
		self.advanced_options.add_integer('PKTSIZE', 'c12.18 maximum packet size', default=512)
		if sys.platform.startswith('linux'):
			self.options.set_option('USECOLOR', 'True')

		# start loading modules
		self.current_module = None
		self.modules = termineter.module.ManagerManager(self, [
			os.path.abspath(os.path.join(__file__, '..', 'modules')),
			os.path.abspath(os.path.join(self.directories.user_data, 'modules'))
		])
		self.logger.info("successfully loaded {0:,} modules into the framework".format(len(self.modules)))
		return

	def __repr__(self):
		return '<' + self.__class__.__name__ + ' Loaded Modules: ' + str(len(self.modules)) + ', Serial Connected: ' + str(self.is_serial_connected()) + ' >'

	def _opt_callback_set_table_cache_policy(self, policy):
		if self.is_serial_connected():
			self.serial_connection.set_table_cache_policy(policy)
		return True

	def reload_module(self, module_path=None):
		"""
		Reloads a module into the framework.  If module_path is not
		specified, then the current_module variable is used.  Returns True
		on success, False on error.

		@type module_path: String
		@param module_path: The name of the module to reload
		"""
		if module_path is None:
			if self.current_module is not None:
				module_path = self.current_module.name
			else:
				self.logger.warning('must specify module if not module is currently being used')
				return False
		if module_path not in self.module:
			self.logger.error('invalid module requested for reload')
			raise termineter.errors.FrameworkRuntimeError('invalid module requested for reload')

		self.logger.info('reloading module: ' + module_path)
		module_instance = self.import_module(module_path, reload_module=True)
		if not isinstance(module_instance, termineter.module.TermineterModule):
			self.logger.error('module: ' + module_path + ' is not derived from the TermineterModule class')
			raise termineter.errors.FrameworkRuntimeError('module: ' + module_path + ' is not derived from the TermineterModule class')
		if not hasattr(module_instance, 'run'):
			self.logger.error('module: ' + module_path + ' has no run() method')
			raise termineter.errors.FrameworkRuntimeError('module: ' + module_path + ' has no run() method')
		if not isinstance(module_instance.options, termineter.options.Options) or not isinstance(module_instance.advanced_options, termineter.options.Options):
			self.logger.error('module: ' + module_path + ' options and advanced_options must be termineter.options.Options instances')
			raise termineter.errors.FrameworkRuntimeError('options and advanced_options must be termineter.options.Options instances')
		module_instance.name = module_path.split('/')[-1]
		module_instance.path = module_path
		self.modules[module_path] = module_instance
		if self.current_module is not None:
			if self.current_module.path == module_instance.path:
				self.current_module = module_instance
		return True

	def run(self, module=None):
		if not isinstance(module, termineter.module.TermineterModule) and not isinstance(self.current_module, termineter.module.TermineterModule):
			raise termineter.errors.FrameworkRuntimeError('either the module or the current_module must be sent')
		if module is None:
			module = self.current_module
		if isinstance(module, termineter.module.TermineterModuleOptical):
			if not self._serial_connected:
				raise termineter.errors.FrameworkRuntimeError('the serial interface is disconnected')

			try:
				self.serial_get()
			except Exception as error:
				self.print_exception(error)
				return
			if module.require_connection and self.advanced_options['AUTOCONNECT']:
				try:
					self.serial_connect()
				except Exception as error:
					self.print_exception(error)
					return
				self.print_good('Successfully connected and the device is responding')
				if not self.serial_login():
					self.logger.warning('meter login failed, some tables may not be accessible')

		self.logger.info('running module: ' + module.path)
		try:
			result = module.run()
		finally:
			if isinstance(module, termineter.module.TermineterModuleOptical) and self.serial_connection and self.advanced_options['AUTOCONNECT']:
				self.serial_connection.stop()
		return result

	@property
	def use_colors(self):
		return self.options['USECOLOR']

	@use_colors.setter
	def use_colors(self, value):
		self.options.set_option('USECOLOR', str(value))

	def get_module_logger(self, name):
		"""
		This returns a logger for individual modules to allow them to be
		inherited from the framework and thus be named appropriately.

		@type name: String
		@param name: The name of the module requesting the logger
		"""
		return logging.getLogger('termineter.module.' + name)

	def import_module(self, module_path, reload_module=False):
		module = self.__package__ + '.modules.' + module_path.replace('/', '.')
		try:
			module = importlib.import_module(module)
			if reload_module:
				importlib.reload(module)
			module_instance = module.Module(self)
		except Exception:
			self.logger.error('failed to load module: ' + module_path, exc_info=True)
			raise termineter.errors.FrameworkRuntimeError('failed to load module: ' + module_path)
		return module_instance

	def print_exception(self, error):
		message = 'Caught ' + error.__class__.__name__ + ': ' + str(error)
		self.logger.error(message, exc_info=True)
		self.print_error(message)

	def print_error(self, message):
		if self.options['USECOLOR']:
			self.stdout.write('\033[1;31m[-] \033[1;m' + (os.linesep + '\033[1;31m[-] \033[1;m').join(message.split(os.linesep)) + os.linesep)
		else:
			self.stdout.write('[-] ' + (os.linesep + '[-] ').join(message.split(os.linesep)) + os.linesep)
		self.stdout.flush()

	def print_good(self, message):
		if self.options['USECOLOR']:
			self.stdout.write('\033[1;32m[+] \033[1;m' + (os.linesep + '\033[1;32m[+] \033[1;m').join(message.split(os.linesep)) + os.linesep)
		else:
			self.stdout.write('[+] ' + (os.linesep + '[+] ').join(message.split(os.linesep)) + os.linesep)
		self.stdout.flush()

	def print_line(self, message):
		self.stdout.write(message + os.linesep)
		self.stdout.flush()

	def print_status(self, message):
		if self.options['USECOLOR']:
			self.stdout.write('\033[1;34m[*] \033[1;m' + (os.linesep + '\033[1;34m[*] \033[1;m').join(message.split(os.linesep)) + os.linesep)
		else:
			self.stdout.write('[*] ' + (os.linesep + '[*] ').join(message.split(os.linesep)) + os.linesep)
		self.stdout.flush()

	def print_hexdump(self, data):
		data_len = len(data)
		i = 0
		while i < data_len:
			self.stdout.write("{0:04x}    ".format(i))
			for j in range(16):
				if i + j < data_len:
					self.stdout.write("{0:02x} ".format(data[i + j]))
				else:
					self.stdout.write('   ')
				if j % 16 == 7:
					self.stdout.write(' ')
			self.stdout.write('   ')
			r = ''
			for j in data[i:i + 16]:
				if 32 < j < 128:
					r += chr(j)
				else:
					r += '.'
			self.stdout.write(r + os.linesep)
			i += 16
		self.stdout.flush()

	def is_serial_connected(self):
		"""
		Returns True if the serial interface is connected.
		"""
		return self._serial_connected

	def serial_disconnect(self):
		"""
		Closes the serial connection to the meter and disconnects from the
		device.
		"""
		if self._serial_connected:
			try:
				self.serial_connection.close()
			except c1218.errors.C1218IOError as error:
				self.logger.error('caught C1218IOError: ' + str(error))
			except serial.serialutil.SerialException as error:
				self.logger.error('caught SerialException: ' + str(error))
			self._serial_connected = False
			self.logger.warning('the serial interface has been disconnected')
		return True

	def serial_get(self):
		"""
		Create the serial connection from the framework settings and return
		it, setting the framework instance in the process.
		"""
		frmwk_c1218_settings = {
			'nbrpkts': self.advanced_options['NBRPKTS'],
			'pktsize': self.advanced_options['PKTSIZE']
		}

		frmwk_serial_settings = termineter.utilities.get_default_serial_settings()
		frmwk_serial_settings['baudrate'] = self.advanced_options['BAUDRATE']
		frmwk_serial_settings['bytesize'] = self.advanced_options['BYTESIZE']
		frmwk_serial_settings['stopbits'] = self.advanced_options['STOPBITS']

		self.logger.info('opening serial device: ' + self.options['CONNECTION'])
		try:
			self.serial_connection = c1218.connection.Connection(self.options['CONNECTION'], c1218_settings=frmwk_c1218_settings, serial_settings=frmwk_serial_settings, enable_cache=self.advanced_options['CACHETBLS'])
		except Exception as error:
			self.logger.error('could not open the serial device')
			raise error
		return self.serial_connection

	def serial_connect(self):
		"""
		Connect to the serial device.
		"""
		self.serial_get()
		try:
			self.serial_connection.start()
		except c1218.errors.C1218IOError as error:
			self.logger.error('serial connection has been opened but the meter is unresponsive')
			raise error
		self._serial_connected = True
		return True

	def serial_login(self):
		"""
		Attempt to log into the meter over the C12.18 protocol. Returns True on success, False on a failure. This can be
		called by modules in order to login with a username and password configured within the framework instance.
		"""
		if not self._serial_connected:
			raise termineter.errors.FrameworkRuntimeError('the serial interface is disconnected')

		username = self.options['USERNAME']
		userid = self.options['USERID']
		password = self.options['PASSWORD']
		if self.options['PASSWORDHEX']:
			hex_regex = re.compile('^([0-9a-fA-F]{2})+$')
			if hex_regex.match(password) is None:
				self.print_error('Invalid characters in password')
				raise termineter.errors.FrameworkConfigurationError('invalid characters in password')
			password = binascii.a2b_hex(password)
		if len(username) > 10:
			self.print_error('Username cannot be longer than 10 characters')
			raise termineter.errors.FrameworkConfigurationError('username cannot be longer than 10 characters')
		if not (0 <= userid <= 0xffff):
			self.print_error('User id must be between 0 and 0xffff')
			raise termineter.errors.FrameworkConfigurationError('user id must be between 0 and 0xffff')
		if len(password) > 20:
			self.print_error('Password cannot be longer than 20 characters')
			raise termineter.errors.FrameworkConfigurationError('password cannot be longer than 20 characters')

		if not self.serial_connection.login(username, userid, password):
			return False
		return True

	def test_serial_connection(self):
		"""
		Connect to the serial device and then verifies that the meter is
		responding.  Once the serial device is open, this function attempts
		to retrieve the contents of table #0 (GEN_CONFIG_TBL) to configure
		the endianess it will use.  Returns True on success.
		"""
		self.serial_connect()

		username = self.options['USERNAME']
		userid = self.options['USERID']
		if len(username) > 10:
			self.logger.error('username cannot be longer than 10 characters')
			raise termineter.errors.FrameworkConfigurationError('username cannot be longer than 10 characters')
		if not (0 <= userid <= 0xffff):
			self.logger.error('user id must be between 0 and 0xffff')
			raise termineter.errors.FrameworkConfigurationError('user id must be between 0 and 0xffff')

		try:
			if not self.serial_connection.login(username, userid):
				self.logger.error('the meter has rejected the username and userid')
				raise termineter.errors.FrameworkConfigurationError('the meter has rejected the username and userid')
		except c1218.errors.C1218IOError as error:
			self.logger.error('serial connection has been opened but the meter is unresponsive')
			raise error

		try:
			general_config_table = self.serial_connection.get_table_data(0)
		except c1218.errors.C1218ReadTableError as error:
			self.logger.error('serial connection as been opened but the general configuration table (table #0) could not be read')
			raise error

		if general_config_table[0] & 1:
			self.logger.info('setting the connection to use big-endian for C12.19 data')
			self.serial_connection.c1219_endian = '>'
		else:
			self.logger.info('setting the connection to use little-endian for C12.19 data')
			self.serial_connection.c1219_endian = '<'

		try:
			self.serial_connection.stop()
		except c1218.errors.C1218IOError as error:
			self.logger.error('serial connection has been opened but the meter is unresponsive')
			raise error

		self.logger.warning('the serial interface has been connected')
		return True
