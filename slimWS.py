import types, sys, traceback, os
import importlib.util
import json
import struct
import logging
from socket import *
from base64 import b64encode
from hashlib import sha1, sha512
from datetime import date, datetime
try:
	from select import epoll, EPOLLIN
except:
	""" #!if windows
	Create a epoll() implementation that simulates the epoll() behavior.
	This so that the rest of the code doesn't need to worry weither or not epoll() exists.
	"""
	import select
	EPOLLIN = None
	class epoll():
		def __init__(self):
			self.sockets = {}
			self.monitoring = {}

		def unregister(self, fileno, *args, **kwargs):
			try:
				del(self.monitoring[fileno])
			except:
				pass

		def register(self, fileno, *args, **kwargs):
			self.monitoring[fileno] = True

		def poll(self, timeout=0.5, *args, **kwargs):
			try:
				return [[fileno, 1] for fileno in select.select(list(self.monitoring.keys()), [], [], timeout)[0]]
			except OSError:
				return []

storage = {}

class PacketIncomplete(Exception):
	"""
	An exception to indicate that the package is not complete.
	This in turn indicates that there *should* come more data down the line,
	altho that's not a guarantee.
	"""
	def __init__(self, message, errors):

		# Call the base class constructor with the parameters it needs
		super().__init__(message)

		# Now for your custom code...
		self.errors = errors

class Events():
	"""
	A static mapper to status codes in the events.
	Each value corresponds to a type of event.

	#TODO: Implement a reverse mapper for string/human
	       readable representation.
	"""
	SERVER_ACCEPT = 0b10000000
	SERVER_CLOSE = 0b10000001
	SERVER_RESTART = 0b00000010

	CLIENT_DATA = 0b01000000
	CLIENT_REQUEST = 0b01000001
	CLIENT_RESPONSE_DATA = 0b01000010
	CLIENT_UPGRADED = 0b01000011
	CLIENT_UPGRADE_ISSUE = 0b01000100
	CLIENT_URL_ROUTED = 0b01000101

	WS_CLIENT_DATA = 0b11000000
	WS_CLIENT_REQUEST = 0b11000001
	WS_CLIENT_COMPLETE_FRAME = 0b11000010
	WS_CLIENT_INCOMPLETE_FRAME = 0b11000011
	WS_CLIENT_ROUTING = 0b11000100
	WS_CLIENT_ROUTED = 0b11000101
	WS_CLIENT_RESPONSE = 0b11000110

	NOT_YET_IMPLEMENTED = 0b00000000

	def convert(_int):
		def_map = {v: k for k, v in Events.__dict__.items() if not k.startswith('__') and k != 'convert'}
		return def_map[_int] if _int in def_map else None

def json_serial(obj):
	"""
	A helper function to being able to `json.dumps()` most things.
	Especially `bytes` data needs to be converted to a `str` object.

	Use this with `default=json_serial` in `json.dumps(default=...)`.

	:param obj: A dictionary object (not the `dict` itself)
	:type obj: Any `dict` compatible `key` or `value`.

	:return: `key` or `value` converted to a `JSON` friendly type.
	:rtype: Any `JSON` compatible `key` or `value`.
	"""
	if isinstance(obj, (datetime, date)):
		return obj.isoformat()
	elif type(obj) is bytes:
		return obj.decode('UTF-8')
	elif getattr(obj, "__dump__", None): #hasattr(obj, '__dump__'):
		return obj.__dump__()
	else:
		return str(obj)

	raise TypeError('Type {} is not serializable: {}'.format(type(obj), obj))

class WS_DECORATOR_MOCK_FUNC():
	"""
	A mockup function to handle the `@app.route` decorator.
	`WebSocket` will call `WS_DECORATOR_MOCK_FUNC.func()` on a frame
	when a frame is recieved, and `@app.route` will return `WS_DECORATOR_MOCK_FUNC.frame`
	in order for the internal cPython to be able to understand what to do.
	"""
	def __init__(self, route, func=None):
		self.route = route
		self.func = func

	def frame(self, f, *args, **kwargs):
		self.func = f

class ModuleError(BaseException):
	pass

class Imported():
	"""
	A wrapper around absolute-path-imported via string modules.
	Supports context wrapping to catch errors.
	Will partially reload *most* of the code in the module in runtime.
	Certain things won't get reloaded fully (this is a slippery dark slope)
	"""
	def __init__(self, server, path, import_id, spec, imported):
		self.server = server
		self.path = path
		self.import_id = import_id # For lookups in virtual.sys.modules
		self.spec = spec
		self.imported = imported
		self.instance = None

	def __enter__(self, *args, **kwargs):
		"""
		It's important to know that it does cause a re-load of the module.
		So any persistant stuff **needs** to be stowewd away.
		Session files *(`pickle.dump()`)* is a good option, or god forbig `__builtin__['storage'] ...` is an option for in-memory stuff.
		"""
		try:
			self.instance = self.spec.loader.exec_module(self.imported)
		except Exception as e:
			exc_type, exc_obj, exc_tb = sys.exc_info()
			fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
			self.server.log(f'Gracefully handled module error in {self.path}: {e}')
			self.server.log(traceback.format_exc())
			raise ModuleError(traceback.format_exc())
		return self

	def __exit__(self, *args, **kwargs):
		# TODO: https://stackoverflow.com/questions/28157929/how-to-safely-handle-an-exception-inside-a-context-manager
		if len(args) >= 2 and args[1]:
			raise args[1]

class _Sys():
	modules = {}

class WebSocket():
	"""
	A simpe API framework to make it a *little bit* easier to
	work with WebSockets and call API endpoints.

	Given a frame with data looking like this:
	```
	    {
	        '_module' : '/auth/login.py',
	        'username' : 'test',
	        'password' : 'secret'
	    }
	```

	The API will look for `./api_modules/auth/login.py` and
	if it exists, it will import and execute it.
	"""
	def __init__(self):
		self.frame_handlers = {}
		self.sys = _Sys()

	def log(self, *args, **kwargs):
		"""
		A stub function for log handling.
		This can be overridden by a client log function to better
		identify where the log data comes from.

		:param *args: Any `str()` valid arguments are valid.
		:type *args: Positional arguments.
		"""
		logger = logging.getLogger(__name__)
		if 'level' in kwargs:
			if type(kwargs['level']) == str:
				if kwargs['level'].lower() == 'critical':
					kwargs['level'] = logging.CRITICAL
				elif kwargs['level'].lower() == 'erro':
					kwargs['level'] = logging.ERROR
				elif kwargs['level'].lower() == 'warning':
					kwargs['level'] = logging.WARNING
				elif kwargs['level'].lower() == 'info':
					kwargs['level'] = logging.INFO
				elif kwargs['level'].lower() == 'debug':
					kwargs['level'] = logging.DEBUG
				# elif kwargs['level'].lower() == 'notset':
				# 	kwargs['level'] = logging.NOTSET
			elif type(kwargs['level']) == int:
				if not kwargs['level'] in (0, 10, 20, 30, 40, 50):
					raise LoggerError(f"Unable to automatically detect the correct log level for: {args} | {kwargs}")
			else:
				raise LoggerError(f"Unknown level definition: {kwargs['level']}")
		else:
			kwargs['level'] = logging.INFO

		logger.log(kwargs['level'], ''.join([str(x) for x in args]))

	def frame(self, *args, **kwargs):
		"""
		A `decorator` function used to re-route the default
		frame parser into something user specific.

		If no specific `URL` was given, it will become the default
		parser for all frames. If a `URL` is specified, only that
		`{'_module' : '/path/to/script.py'}` path in `_module` can
		trigger the frame handler. This is so that "in memory" files
		can be created instead of storing them on disk.

		:param URL: A path in which `_module` paths are compared against.
		:type URL: str optional

		:return: Returns a decorator mock function
		:rtype: slimWS.WS_DECORATOR_MOCK_FUNC.frame
		"""
		self.frame_func = args[0]
		#self.frame_handlers[URL] = WS_DECORATOR_MOCK_FUNC(URL)
		#return self.frame_handlers[URL].frame

	def WS_CLIENT_IDENTITY(self, request):
		"""
		Sets up a new :class:`~slimWS.WS_CLIENT_IDENTITY` for a specific request.
		Sets its own instance log function to the requests server instance to
		make sure all logs are routed to a one-stop-shop function.

		# TODO: Make sure the server actually has a log function tho heh

		:param request: A HTTP request object which must include
		    a valid `CLIENT_IDENTITY` object. For instance :class:`~slimWS.WS_CLIENT_IDENTITY`.
		:type request: `HTTP_REQUEST` object.

		:return: Returns a :class:`~slimWS.WS_CLIENT_IDENTITY` initialized object.
		:rtype: :class:`~slimWS.WS_CLIENT_IDENTITY`
		"""
		self.log=request.CLIENT_IDENTITY.server.log
		return WS_CLIENT_IDENTITY(request, server=self)

	def find_final_module_path(self, path, frame):
		"""
		Simply checks if a :class:`~slimWS.WS_FRAME`'s `_module` path exists.
		If not, it returns `False`.

		:param path: A path to look for API endpoints/modules.
		:type path: str
		:param frame: A :class:`~slimWS.WS_FRAME` instance with the data `{'_module' : '/path/here.py'}`
		:type frame: :class:`~slimWS.WS_FRAME`

		:return: Returns the full path to a successful module/api endpoint.
		:rtype: str
		"""
		full_path = f"{path}/{frame.data['_module']}.py"
		if os.path.isfile(full_path):
			return os.path.abspath(full_path)

	def importer(self, path):
		"""
		In charge of importing (and executing) `.py` modules.
		Usually called upon when a module/api is being loaded.
		Not something individual developers need to worry about.

		:param path: A path to import python files.
		:type path: str

		:return: Returns if an old version was called, as well as the `spec` for the file.
		:rtype: tuple
		"""

		

		old_version = False
		self.log(f'Request to import "{path}"', level=6, origin='importer')
		if path not in self.loaded_apis:
			## https://justus.science/blog/2015/04/19/sys.modules-is-dangerous.html
			try:
				self.log(f'Loading API module: {path}', level=4, origin='importer')
				spec = importlib.util.spec_from_file_location(path, path)
				self.loaded_apis[path] = importlib.util.module_from_spec(spec)
				spec.loader.exec_module(self.loaded_apis[path])
				sys.modules[path] = self.loaded_apis[path]
			except (SyntaxError, ModuleNotFoundError) as e:
				self.log(f'Failed to load API module ({e}): {path}', level=2, origin='importer')
				return None
		else:
			try:
				raise SyntaxError('https://github.com/Torxed/ADderall/issues/11')
			except SyntaxError as e:
				old_version = True

		return old_version, self.loaded_apis[f'{path}']

	def uniqueue_id(self, seed_len=24):
		"""
		Generates a unique identifier in 2020.
		TODO: Add a time as well, so we don't repeat the same 24 characters by accident.
		"""
		return sha512(os.urandom(seed_len)).hexdigest()

	def frame_func(self, frame):
		"""
		The function called on when a processed frame should be parsed.
		When a frame reaches this stage, it's important that it's fully
		complete and defragmented and XOR:ed if that's nessecary.

		This is the `default` handler for frames, which can be overriden by
		calling the decorator `@app.frame`.

		:param frame: a :class:`~slimWS.WS_FRAME` which have been processed.
		:type frame: :class:`~slimWS.WS_FRAME`

		:return: Will `yield` a API friendly struct from the `<module.py>.parser().process`
		    that looks like this: `{**process-items, '_uid' : None|`data['uid'], '_modules' : data['_module']}`
		:rtype: iterator
		"""
		if type(frame.data) is not dict or '_module' not in frame.data:
			self.log(f'Invalid request sent, missing _module in JSON data: {str(frame.data)[:200]}')
			return

		# TODO: If empty, fallback to finding automatically.
		#       Buf it NOT empty, do not automatically find modules unless specified.
		if frame.data['_module'] in self.frame_handlers:
			response = self.frame_handlers[frame.data['_module']].func(frame)
			yield (Events.WS_CLIENT_RESPONSE, response)
		else:
			## TODO: Add path security!
			## TODO: Actually test this out, because it hasn't been run even once.
			module_to_load = self.find_final_module_path('./api_modules', frame)
			if(module_to_load):
				if not module_to_load in self.sys.modules:
					spec = importlib.util.spec_from_file_location(module_to_load, module_to_load)
					imported = importlib.util.module_from_spec(spec)
					
					import_id = self.uniqueue_id()
					self.sys.modules[module_to_load] = Imported(frame.CLIENT_IDENTITY.server, module_to_load, import_id, spec, imported)
					sys.modules[import_id+'.py'] = imported

				try:
					with self.sys.modules[module_to_load] as module:
						# We have to re-check the @.route definition after the import, since it *might* have changed
						# due to imports being allowed to do @.route('/', vhost=this)
						#if self.vhost in frame.CLIENT_IDENTITY.server.routes and self.headers[b'URL'] in frame.CLIENT_IDENTITY.server.routes[self.vhost]:
						#	yield (Events.CLIENT_URL_ROUTED, frame.CLIENT_IDENTITY.server.routes[self.vhost][self.headers[b'URL']].parser(self))

						if hasattr(module.imported, 'on_request'):
							if (module_data := module.imported.on_request(frame)):
								if isinstance(module_data, types.GeneratorType):
									for data in module_data:
										yield (Events.WS_CLIENT_RESPONSE, data)
								else:
									yield (Events.WS_CLIENT_RESPONSE, module_data)
				except ModuleError as e:
					self.log(f'Module error in {module_to_load}: {e}')
					frame.CLIENT_IDENTITY.close()

				except BaseException as e:
					exc_type, exc_obj, exc_tb = sys.exc_info()
					fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
					self.log(f'Exception in {fname}@{exc_tb.tb_lineno}: {e} ')
					self.log(traceback.format_exc(), level=2, origin='pre_parser', function='parse')
			else:
				self.log(f'Invalid data, trying to load a inexisting module: {frame.data["_module"]} ({str(frame.data)[:200]})')


	def post_process_frame(self, frame):
		"""
		Post processing of a frame before it's delivered to `@app.frame` function.
		This is the last step between assembling the frame and the developer getting the frame.

		:param frame: a :class:`~slimWS.WS_FRAME` which have been assembled.
		:type frame: :class:`~slimWS.WS_FRAME`

		:return: Will `yield` a :class:`~slimWS.Events` and event data.
		:rtype: iterator
		"""
		try:
			frame.data = json.loads(frame.data.decode('UTF-8'))
			for data in self.frame_func(frame):
				if type(data) == dict:
					try:
						data = json.dumps(data)
					except Exception as e:
						print('Could not dump JSON:', e)
						data = str(data)
				if type(data) == str:
					data = bytes(data, 'UTF-8')
				yield (Events.WS_CLIENT_RESPONSE, data)
		except UnicodeDecodeError:
			self.log('[ERROR] UnicodeDecodeError:', frame.data)
			return None
		except Exception as e:
			exc_type, exc_obj, exc_tb = sys.exc_info()
			fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
			self.log(f'Error in post processing frame {fname}@{exc_tb.tb_lineno}:', frame.data)
			self.log(traceback.format_exc(), level=3)
			return None

class WS_CLIENT_IDENTITY():
	"""
	A valid `IDENTITY` identifier within the `slim` family.
	This correlates to `slimHTTP`'s `HTTP_CLIENT_IDENTITY` and enables
	seamless handling between the product family.

	:param request: a `request` which holds a valid client identity of any sort,
	    such as :class:`~slimWS.WS_CLIENT_IDENTITY`.
	:type request: `request`
	:param server: A server instance, if `None` it will default to the :ref:`slimWS.WS_CLIENT_IDENTITY.server`
	:type server: :class:`~slimWS.WS_SERVER`
	"""
	def __init__(self, request, server=None):
		if not server: server = request.CLIENT_IDENTITY.server
		self.server = server
		self.socket = request.CLIENT_IDENTITY.socket
		self.fileno = request.CLIENT_IDENTITY.fileno
		self.address = request.CLIENT_IDENTITY.address
		self.keep_alive = request.CLIENT_IDENTITY.keep_alive
		self.buffer_size = 8192
		self.closing = False

		self.buffer = request.CLIENT_IDENTITY.buffer # Take over the buffer
		self.on_close = request.CLIENT_IDENTITY.on_close

		self.virtual_host = None
		if b'host' in request.headers:
			try:
				self.virtual_host = request.headers[b'host'].decode('UTF-8')
			except:
				pass

	def upgrade(self, request):
		"""
		A function which can be called to upgrade a `X_CLIENT_IDENTITY` to a
		:class:`~slimWS.WS_CLIENT_IDENTITY`.

		Used in junction with `slimHTTP` requests where the `Upgrade: websocket` header
		is present. And the return value replaces `slimHTTP.HTTP_CLIENT_IDENTITY` with a
		:class:`~slimWS.WS_CLIENT_IDENTITY` which shares the same bahviors, just different results.

		:param request: a `request` which holds a valid client identity of any sort,
		    such as :class:`~slimWS.WS_CLIENT_IDENTITY`.
		:type request: `request`

		:return: (TBD) Will return a :class:`~slimWS.WS_CLIENT_IDENTITY` instance.
		:rtype: :class:`~slimWS.WS_CLIENT_IDENTITY`
		"""
		self.server.log(f'Performing upgrade() response for: {self}', level=5, source='WS_CLIENT_IDENTITY.upgrade()')
		self.keep_alive = True

		if b'host' in request.headers:
			try:
				self.virtual_host = request.headers[b'host'].decode('UTF-8')
			except:
				pass

		if not b'sec-websocket-key' in request.headers:
			self.close()

		magic_key = request.headers[b'sec-websocket-key'] + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
		magic_hash = sha1(magic_key).digest()

		resp = b''
		resp += b'HTTP/1.1 101 Switching protocols\r\n'
		resp += b'Upgrade: websocket\r\n'
		resp += b'Connection: Upgrade\r\n'
		resp += b'Sec-WebSocket-Accept: ' + b64encode(magic_hash) + b'\r\n'
		resp += b'\r\n'

		self.socket.send(resp)
		self.buffer = b''

		self.server.log(f'{self}\'s upgrade response is sent.', level=5, source='WS_CLIENT_IDENTITY.upgrade()')

	def close(self):
		"""
		Will call the `on_close` event which by default will set off a process where
		the socket starts to be unregistered from the main poll object, after which
		the socket itself will be closed.
		"""
		if not self.closing:
			self.closing = True
			self.on_close(self)

	def on_close(self, *args, **kwargs):
		"""
		An event that is being called when a socket either requests to be closed,
		or malicious data was recieved.. or simply the developer closes the client.
		"""
		if not self.closing:
			self.server.on_close_func(self)

	def poll(self, timeout=0.2, force_recieve=False):
		"""
		The `poll` function checks if there's incomming **data from this specific client**.
		This will call `server.poll(timeout, fileno=self)` to filter out data on this specific client.

		# TODO: Remove old logic of force_recieve as it's never used and have no meaning here.

		:param timeout: Defaults to a 0.2 second timeout. The timeout simply states
		    how long to poll for client data in this poll window. We'll gather a list
		    of all clients sending data within this `timeout` window.
		:type timeout: float optional
		:param force_recieve: If the caller knows there's data, we can override
		    the polling event and skip straight to data recieving.
		:type force_recieve: bool

		:return: Returns a tuple of (Events.WS_CLIENT_DATA, len(buffer))
		:rtype: tuple
		"""
		if force_recieve or list(self.server.poll(timeout, fileno=self.fileno)):
			try:
				d = self.socket.recv(self.buffer_size)
			except: # There's to many errors that can be thrown here for differnet reasons, SSL, OSError, Connection errors etc.
			        # They all mean the same thing, things broke and the client couldn't deliver data accordingly so eject.
				d = ''

			if len(d) == 0:
				return self.on_close(self)

			self.buffer += d
			yield (Events.CLIENT_DATA, len(self.buffer))

	def send(self, data):
		"""
		A wrapper for the `socket` on :class:`~slimWS.WS_CLIENT_IDENTITY`.
		It will however call `ws_send` instead which is a frame-builder for the
		web socket protocol.

		`send()` will however try to convert the data before passing it to `ws_send`.

		.. warning:: `dict` objects will be converted with `json.dumps()` using :func:`~slimWS.json_serial`.

		:param data: `bytes`, `json.dumps` or `str()` friendly objects.
		:type data: bytes

		:return: The length of the buffer that was actually sent
		:rtype: int
		"""

		if type(data) == dict:
			data = json.dumps(data, default=json_serial)
		if type(data) != bytes:
			data = bytes(str(data), 'UTF-8')

		try:
			return self.ws_send(data)
		except:
			return 0

	def ws_send(self, data, SPLIT=False):
		"""
		Sends any given `bytes` data to the socket of the :class:`~slimWS.WS_CLIENT_IDENTITY`.

		:param data: Has to be encoded `bytes` data.
		:type data: bytes

		:return: The length of the buffer that was actually sent
		:rtype: int
		"""

		#log('[Structuring a packet]', level=5, origin='spiderWeb', function='ws_send')

		if SPLIT: data = [data[0+i:SPLIT+i] for i in range(0, len(data), SPLIT)] # Might get in trouble if splitting to 126 or 127. TODO: investigate.
		else: data = [data]

		for index, segment in enumerate(data):
			packet = b''
			## Add the flags + opcode (0 == continuation frame, 1 == text, 2 == binary, 8 == con close, 9 == ping, A == pong)
			last_segment = True if index == len(data)-1 else False
			fin, rsv1, rsv2, rsv3, opcode = (b'1' if last_segment else b'0'), b'0', b'0', b'0', bytes('{:0>4}'.format('1' if last_segment else '0'), 'UTF-8') # .... (....)
			#log(b''.join([fin, rsv1, rsv2, rsv3, opcode]).decode('UTF-8'), level=5, origin='spiderWeb', function='ws_send')
			packet += struct.pack('B', int(b''.join([fin, rsv1, rsv2, rsv3, opcode]), 2))

			mask = b'0'
			if mask == b'1':
				mask_key = b'\x00\x00\x00\x00' # We can also use b'' and mask 0. But it'll work the same.
				for index, c in enumerate(segment):
					pass # Do something with the data.
			else:
				mask_key = b''
			payload_len = len(segment)
			extended_len = 0

			if payload_len > 65535: # If the len() is larger than a 2 byte INT
				extended_len = struct.pack('!Q', payload_len)
				payload_len = struct.pack('B', int(b''.join([mask, bytes('{0:0>7b}'.format(127), 'UTF-8')]),2))

			elif payload_len >= 126: # 2 bytes INT
				extended_len = struct.pack('!H', payload_len)
				#log(b'[Mask]:'.join([mask, bytes('{0:0>7b}'.format(126), 'UTF-8')]), level=5, origin='spiderWeb', function='ws_send')
				payload_len = struct.pack('B', int(b''.join([mask, bytes('{0:0>7b}'.format(126), 'UTF-8')]),2))
			else:
				extended_len = b''
				#log(b''.join([mask, bytes('{0:0>7b}'.format(payload_len), 'UTF-8')]).decode('UTF-8'), end=' ', level=5, origin='spiderWeb', function='ws_send')
				payload_len = struct.pack('B', int(b''.join([mask, bytes('{0:0>7b}'.format(payload_len), 'UTF-8')]),2))

			# Payload len is padded with mask
			packet += payload_len + extended_len + mask_key + segment
			#log('[Flags::Data]:', {'fin': fin, 'rsv1': rsv1, 'rsv2': rsv2, 'rsv3': rsv3, 'mask': True if mask == b'1' else False, 'opcode':opcode, 'len' : len(segment), 'mask_key' : mask_key}, segment, level=10, origin='spiderWeb', function='ws_send')

			#log(data.decode('UTF-8'), origin='spiderWeb', function='ws_send')
			#log('[Final::Data]:', packet, level=10, origin='spiderWeb', function='ws_send')
			self.socket.send(packet)

	def build_request(self):
		"""
		A function which can be called to build a :class:`~slimWS.WS_FRAME` instance.

		:return: Returns a tuple of (Events.CLIENT_REQUEST, :class:`~slimWS.WS_FRAME(self)`)
		:rtype: iterator
		"""
		yield (Events.CLIENT_REQUEST, WS_FRAME(self))

	def has_data(self):
		"""
		:return: Returns `True` if the length of the client's buffer is >0
		:rtype: bool
		"""
		if self.closing: return False
		return True if len(self.buffer) else False

	def __repr__(self):
		return f'<slimWS.WS_CLIENT_IDENTITY @ {self.address}>'

class WS_FRAME():
	"""
	A frame handler for any data in a :class:`~slimWS.WS_CLIENT_IDENTITY` buffer.
	The frame itself does not retrieve/send data, it simply takes a pre-defined buffer
	of the :class:`~slimWS.WS_CLIENT_IDENTITY` instance and processes it.

	:param CLIENT_IDENTITY: A valid `CLIENT_IDENTITY` identifier within the `slim` family.
	:type CLIENT_IDENTITY: :class:`~slimWS.WS_CLIENT_IDENTITY`
	"""
	def __init__(self, CLIENT_IDENTITY):
		self.CLIENT_IDENTITY = CLIENT_IDENTITY

	def build_request(self):
		yield (Events.CLIENT_REQUEST, self)

	def parse(self):
		"""
		Retrieves and parses the :ref:`~slimWS.WS_CLIENT_IDENTITY.buffer`.
		If an incomplete frame was recieved, it yield *(not raise)* :class:`~slimWS.PacketIncomplete` event.

		:return: (:ref:`~slimWS.Events.WS_CLIENT_COMPLETE_FRAME`, :ref:`~slimWS.WS_FRAME`)
		:rtype: iterator
		"""

		flag_bits = self.CLIENT_IDENTITY.buffer[0]
		mask_and_len = self.CLIENT_IDENTITY.buffer[1]

		# Make sure we're reading Opcode: Text (1)
		# Or that we got Opcode: Connection Close (8)
		try:
			assert bool(flag_bits & 0b00000001) or \
					bool(flag_bits & 0b00001000)
		except:
			print('  Flag bits not implemented:', flag_bits)
			self.CLIENT_IDENTITY.buffer = b''
			self.data = None
			return None

		self.flags = {}
		self.flags['fin'] = bool(flag_bits & 0b10000000)
		self.flags['mask'] = bool(mask_and_len & 0b10000000)
		self.flags['payload_length'] = mask_and_len & 0b01111111

		header_length = 6
		if self.flags['payload_length'] < 126:
			self.mask_key = self.CLIENT_IDENTITY.buffer[2:6]
			payload = self.CLIENT_IDENTITY.buffer[header_length:header_length+self.flags['payload_length']]
		elif self.flags['payload_length'] == 126:
			self.flags['payload_length'] = struct.unpack('>H', self.CLIENT_IDENTITY.buffer[2:4])[0]
			self.mask_key = self.CLIENT_IDENTITY.buffer[4:8]
			header_length = 8
			payload = self.CLIENT_IDENTITY.buffer[header_length:header_length+self.flags['payload_length']]
		elif self.flags['payload_length'] == 127:
			self.flags['payload_length'] = struct.unpack('>Q', self.CLIENT_IDENTITY.buffer[2:10])[0]
			self.mask_key = self.CLIENT_IDENTITY.buffer[10:14]
			header_length = 14
			payload = self.CLIENT_IDENTITY.buffer[header_length:header_length+self.flags['payload_length']]

		if len(payload) < self.flags['payload_length']:
			# We do not have all the data yet from our own underlaying socket layer
			return None

		self.CLIENT_IDENTITY.buffer = self.CLIENT_IDENTITY.buffer[header_length+self.flags['payload_length']:]

		if bool(flag_bits & 0b00001000):
			self.data = None
			return None

		if self.flags['mask']:
			self.data = b''
			for index, c in enumerate(payload):
				self.data += bytes([c ^ self.mask_key[(index%4)]])

			if self.data == b'PING':
				#self.ws_send(b'Pong') #?
				self.data = None
				return

			if len(self.data) < self.flags['payload_length']:
				self.complete_frame = False
				## Ok so, TODO:
				#  This fucking code shouldn't need to be here...
				#  I don't know WHY handler.poll() in core.py doesn't work as it should,
				#  but it should return a fileno+event when there's more data to the payload.
				#  But for whatever reason, handler.poll() returms empty even tho there's more data
				#  to be collected for this websocket-packet.. and doing recv() again does actually
				#  return more data - epoll() just don't know about it for some reason.
				#  So the only way to do this, is to raise PacketIncomplete() and let outselves
				#  trigger another recv() manually from within this parse-session.
				#  The main issue with this tho, is that someone could send a fake length + not send data.
				#  Effectively blocking this entire application.. I don't like this one bit..
				#  But for now, fuck it, just keep the project going and we'll figure out why later!
				#  (FAMOUSE LAST WORDS)
				#raise PacketIncomplete("Incomplete packet.", (self.flags['payload_length'], len(self.carryOver)))
			else:
				self.complete_frame = True
				if self.flags['fin']:
					#if self.fragmented:
					#	self.fragmented = False # TODO: needed?
					#else:
					#	self.convert_to_json() # Also used internally for normal data
					yield (Events.WS_CLIENT_COMPLETE_FRAME, self)

					for subevent, entity in self.CLIENT_IDENTITY.server.post_process_frame(self):
						yield subevent, entity
				else:
					yield (Events.WS_CLIENT_INCOMPLETE_FRAME, self)
