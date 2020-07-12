# slimWS
WebSocket framework writtein in Python.<br>
Works standalone but is preferred as `@upgrader` for [slimHTTP](https://github.com/Torxed/slimHTTP). 

# Installation

### pypi

    pip install slimWS

### Git it to a project

    git submodule add -b master https://github.com/Torxed/slimWS.git 

*(Or just `git clone https://github.com/Torxed/slimWS.git`)*

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