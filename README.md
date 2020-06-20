# spiderWeb
WebSocket API engine

# Example usage

```python
from spiderWeb import spiderWeb

server = spiderWeb.host(address='', port=4001)

@server.route_parser
def parse(self, frame):
	print('Got WebSocket frame:', frame.data)
	yield {'status' : 'successful'}
```
# Modules

 * [jwt](https://github.com/Torxed/spiderWeb-jwt)
 * oauth2
 * slimAUTH
