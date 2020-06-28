# slimWS
WebSocket API engine.<br>
Works best with [slimHTTP](https://github.com/Torxed/slimHTTP) although it does work all on its own. 

# Example usage

```python
from slimWS import slimws

server = slimws.host(address='', port=4001)

@server.route_parser
def parse(self, frame):
	print('Got WebSocket frame:', frame.data)
	yield {'status' : 'successful'}

while 1:
	for event, *event_data in server.poll():
		pass
```
# Modules

 * [jwt](https://github.com/Torxed/spiderWeb-jwt)
 * oauth2
 * slimAUTH
