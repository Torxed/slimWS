# spiderWeb
WebSocket API engine

# Example usage

	from spiderWeb import spiderWeb

	class parser():
		def parse(self, client, data, headers, fileno, addr, *args, **kwargs):
			print(data)

	server = spiderWeb.server({'default' : parser()}, address='', port=4001)



# Modules

 * [jwt](https://github.com/Torxed/spiderWeb-jwt)
 * oauth2
 * slimAUTH
