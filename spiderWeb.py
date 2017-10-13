from socket import *
from base64 import b64encode
from hashlib import sha1
from json import loads, dumps
from select import epoll, EPOLLIN
from struct import pack, unpack

from helpers import *
from api import gh_api
from api import crispy

# B = 1
# H = 2
# I = 4
# Q = 8

def list_to_dict(_list_):
	# TODO: Verify that the length is dividable with 2.
	a, b = _list_[:len(_list_)//2], _list_[len(_list_)//2:]
	result = {}
	for index, item in enumerate(a):
		result[item] = b[index]
	return result

class ws_client():
	def __init__(self, sock, addr, parsers={}):
		self.sock = sock
		self.addr = addr
		self.state = 'HTTP'
		self.data = b''
		self.closed = False

		self.ws_packet = {'flags' : {'fin' : None, 'rsv1' : None, 'rsv2' : None, 'rsv3' : None, 'mask' : None},
				'optcode' : None,
				'payload_len' : 0,
				'mask_key' : None}
		self.ws_index = 0

		self.headers, self.payload = None, None
		self.parsers = parsers

	def http_parse(self, d):
		if len(d) <= 0:
			return {}, b''
		headers = {}
		header, payload = d.split(b'\r\n\r\n',1)
		rows = header.split(b'\r\n')
		headers[b'REQUEST'] = rows[0].split(b' ',2)[1]

		for row in rows[1:]:
			key, val = row.split(b':',1)
			headers[key.strip(b' \\;,.\r\n')] = val.strip(b' \\;,.\r\n')
		return headers, payload

	def ws_send(self, data, SPLIT=False):
		log('\n[Structuring a packet]')

		if type(data) == dict:
			data = dumps(data)
		if type(data) != bytes:
			data = bytes(str(data), 'UTF-8')

		if SPLIT: data = [data[0+i:SPLIT+i] for i in range(0, len(data), SPLIT)] # Might get in trouble if splitting to 126 or 127. TODO: investigate.
		else: data = [data]

		for index, segment in enumerate(data):
			packet = b''
			## Add the flags + optcode (0 == continuation frame, 1 == text, 2 == binary, 8 == con close, 9 == ping, A == pong)
			last_segment = True if index == len(data)-1 else False
			fin, rsv1, rsv2, rsv3, optcode = (b'1' if last_segment else b'0'), b'0', b'0', b'0', bytes('{:0>4}'.format('1' if last_segment else '0'), 'UTF-8') # .... (....)
			#log(b''.join([fin, rsv1, rsv2, rsv3, optcode]).decode('UTF-8'), end=' ')
			packet += pack('B', int(b''.join([fin, rsv1, rsv2, rsv3, optcode]), 2))

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
				extended_len = pack('!Q', payload_len)
				payload_len = pack('B', int(b''.join([mask, bytes('{0:0>7b}'.format(127), 'UTF-8')]),2))

			elif payload_len >= 126: # 2 bytes INT
				extended_len = pack('!H', payload_len)
				log(b''.join([mask, bytes('{0:0>7b}'.format(126), 'UTF-8')]))
				payload_len = pack('B', int(b''.join([mask, bytes('{0:0>7b}'.format(126), 'UTF-8')]),2))
			else:
				extended_len = b''
				#log(b''.join([mask, bytes('{0:0>7b}'.format(payload_len), 'UTF-8')]).decode('UTF-8'), end=' ')
				payload_len = pack('B', int(b''.join([mask, bytes('{0:0>7b}'.format(payload_len), 'UTF-8')]),2))

			# Payload len is padded with mask
			packet += payload_len + extended_len + mask_key + segment
			log('[Flags::Data]:', {'fin': fin, 'rsv1': rsv1, 'rsv2': rsv2, 'rsv3': rsv3, 'mask': True if mask == b'1' else False, 'OpCode':optcode, 'len' : len(segment), 'mask_key' : mask_key}, segment)

			#log(data.decode('UTF-8'))
			log('[Final::Data]:', packet)
			self.sock.send(packet)

	def recv(self, buf=8192):
		self.data += self.sock.recv(buf)

		if len(self.data) == 0:
			log('[Socket disconnected]')
			self.sock.close()
			self.closed = True
			return

		if b'\r\n\r\n' in self.data and self.state == 'HTTP':
			self.headers, self.payload = self.http_parse(self.data)
			print(self.headers)

			if b'Sec-WebSocket-Key' in self.headers and \
			  b'Upgrade' in self.headers and b'Connection' in self.headers and \
			  self.headers[b'Upgrade'].lower() == b'websocket' and \
			  b'upgrade' in self.headers[b'Connection'].lower():
				magic_key = self.headers[b'Sec-WebSocket-Key'] + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
				hash = sha1(magic_key).digest()

				resp = b''
				resp += b'HTTP/1.1 101 Switching protocols\r\n'
				resp += b'Upgrade: websocket\r\n'
				resp += b'Connection: Upgrade\r\n'
				resp += b'Sec-WebSocket-Accept: ' + b64encode(hash) + b'\r\n'
				resp += b'\r\n'

				log('[Connection upgraded]')
				self.data = b''
				self.ws_index = 0
				self.state = 'WEBSOCK'

				self.sock.send(resp)
			else:
				resp = b'HTTP/1.1 404 Not Found\r\n\r\n'
				self.sock.send(resp)
				self.sock.close()
				self.closed = True

		elif self.state == 'WEBSOCK':
			log('\n[Parsing packet]:', self.data)
			if len(self.data) >= 1 and self.ws_packet['flags']['fin'] is None:
				flags = '{0:0>8b}'.format(unpack('B', bytes([self.data[self.ws_index]]))[0])
				fin, rsv1, rsv2, rsv3 = [True if x == '1' else False for x in flags[0:4]]
				opcode = int(flags[4:8], 2)
				self.ws_packet['flags']['fin'] = fin
				self.ws_packet['flags']['rsv1'] = fin
				self.ws_packet['flags']['rsv2'] = fin
				self.ws_packet['flags']['rsv3'] = fin
				self.ws_packet['optcode'] = opcode
				log('Flags:', self.ws_packet['flags'])
				log('OpCode:', self.ws_packet['optcode'])
				self.ws_index += 1

			if len(self.data) >= 2 and self.ws_packet['payload_len'] == 0:
				self.ws_packet['payload_len'] = unpack('B', bytes([self.data[self.ws_index]]))[0]
				self.ws_packet['payload_len'] = '{0:0>8b}'.format(self.ws_packet['payload_len'])
				self.ws_packet['flags']['mask'], self.ws_packet['payload_len'] = (True if self.ws_packet['payload_len'][0] == '1' else False), int('{:0>8}'.format(self.ws_packet['payload_len'][1:]), 2)
				self.ws_index += 1

			if self.ws_packet['payload_len'] < 126:
				log('Len:', self.ws_packet['payload_len'])

			elif self.ws_packet['payload_len'] == 126 and len(self.data) >= self.ws_index+2:
				extended_payload_len = unpack('H', self.data[self.ws_index:self.ws_index+2])[0]
				self.ws_index += 2
				log('Large payload:', extended_payload_len)
			elif self.ws_packet['payload_len'] >= 127 and len(self.data) >= self.ws_index+8:
				extended_payload_len = unpack('Q', self.data[self.ws_index:self.ws_index+8])[0]
				self.ws_index += 8
				log('Huge payload:', extended_payload_len)

			if self.ws_packet['flags']['mask'] and len(self.data) >= self.ws_index+4:
				#mask_key = unpack('I', ws[parsed_index:parsed_index+4])[0]
				self.ws_packet['mask_key'] = self.data[self.ws_index:self.ws_index+4]
				log('Mask key:', self.ws_packet['mask_key'])
				self.ws_index += 4

			if self.ws_packet['flags']['mask']:
				data = b''
				log('[XOR::Data]:', self.data[self.ws_index:])
				for index, c in enumerate(self.data[self.ws_index:]):
					data += bytes([c ^ self.ws_packet['mask_key'][(index%4)]])

				log('[   ::Data]:', data)
				#self.ws_send(b'Pong')
				try:
					data = loads(data.decode('UTF-8'))
				except UnicodeDecodeError:
					self.sock.close()
					self.closed = True
					return 

				for parser in self.parsers:
					data = self.parsers[parser].protocol.parse(data, self.headers, self.sock.fileno(), self.addr)
					self.ws_send(data)

			if self.ws_packet['flags']['fin']:
				#self.sock.close()
				#self.closed = True
				self.data = b'' # Does not support multiple buffered blocks (see if mask:)
				self.ws_index = 0
				self.ws_packet = {'flags' : {'fin' : None, 'rsv1' : None, 'rsv2' : None, 'rsv3' : None, 'mask' : None},
						  'optcode' : None,
						  'payload_len' : 0,
						  'mask_key' : None}

s = socket()
s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)

s.bind(('', 8010))
s.listen(4)

clients = {}
lookup = {s.fileno():'#Server'}
poller = epoll()
poller.register(s.fileno(), EPOLLIN)

parsers = {'api:gh' : gh_api, 'api:crispy' : crispy} ## == Examples, could be anything that will have the format `class protocol().parse(payload, headers, socket_fileno, socket_address)`

while 1:
	for fileno, eventID in poller.poll(10):
		log('\nSock event:', translation_table[eventID] if eventID in translation_table else eventID, lookup[fileno] if fileno in lookup else fileno)
		if fileno == s.fileno():
			ns, na = s.accept()
			poller.register(ns.fileno(), EPOLLIN)
			clients[ns.fileno()] = {'socket' : ws_client(ns, na, parsers), 'user' : None, 'domain' : None}
			lookup[ns.fileno()] = na

		elif fileno in clients:
			if clients[fileno]['socket'].closed:
				log('#Closing fileno:', fileno)
				poller.unregister(fileno)
				del clients[fileno]
				del lookup[fileno]
			else:
				clients[fileno]['socket'].recv()
		else:
			log('Fileno not in clients?')


