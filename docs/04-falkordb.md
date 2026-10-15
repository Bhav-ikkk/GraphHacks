# FalkorDB

The browser and the database are two different ports. The Python client speaks the Redis protocol to the database. It does not log in through the website.

## What the FalkorDB docs show

The quickstart on the FalkorDB site connects like this, then runs Cypher:

```python
from falkordb import FalkorDB

client = FalkorDB(host="localhost", port=6379)
graph = client.select_graph("social")

graph.query("""
CREATE (alice:User {id: 1, name: "Alice", email: "alice@example.com"})
CREATE (bob:User {id: 2, name: "Bob", email: "bob@example.com"})
""")

rows = graph.ro_query("""
MATCH (alice:User {name: 'Alice'})-[:FRIENDS_WITH]->(friend)
RETURN friend.name AS Friend
""").result_set
```

That sample is a social graph (`User`, `Post`, `FRIENDS_WITH`, `CREATED`) used to teach the client. This project does not load Alice, Bob, or that schema. The same client calls are used to store the engineering graph: services, functions, dependencies, owners, and tests.

With a password, the docs say to pass it in the constructor. An empty username means we omit it (`None`), which matches "leave the name empty":

```python
client = FalkorDB(
    host=host,
    port=port,
    username=None,
    password=password,
)
graph = client.select_graph(graph_name)
```

Writes use `graph.query`. Reads use `graph.ro_query`, and the rows are `result.result_set`. That is what `edl/falkor.py` does.

Labels and relationship types are taken from a fixed allow-list (`Service`, `Function`, `DEPENDS_ON`, `OWNS`, and the others in that file). A value that is not a string, number, or boolean is not sent. The page never accepts a Cypher string from you.

## What you put in `.env`

`.env` is gitignored. `.env.example` shows the keys with an empty password.

| Key | Meaning |
| --- | --- |
| `FALKORDB_HOST` | Database host. Not the website |
| `FALKORDB_PORT` | Database port. The Docker quickstart uses `6379` |
| `FALKORDB_USERNAME` | Leave empty |
| `FALKORDB_PASSWORD` | The password you create. Never commit it |
| `FALKORDB_GRAPH` | Graph name to create or replace. The sample docs use `social`. This project writes the repository graph under whatever name you set |

The browser you already have open is [http://13.236.193.238:3000/login](http://13.236.193.238:3000/login). Port 3000 returned a web page. Putting `3000` in `FALKORDB_PORT` will not work, because the client is not a browser.

If you created a FalkorDB Cloud instance, copy the host, port, and password from that instance's connection panel into these three fields. Cloud hosts often look like a hostname rather than the browser IP. The username field on that panel can stay blank in `.env`.

## What happens when a repository is analyzed

1. Ingest builds the in-memory graph.
2. `publish` connects and calls `select_graph` with `FALKORDB_GRAPH`.
3. That graph is deleted and rewritten, so the browser shows this repository and not a previous one.
4. A count query checks how many nodes and edges landed, and the service names are read back.
5. The header chip shows the browser address, the graph name, the counts, and those service names.

Replacing the graph is deliberate. If `FALKORDB_GRAPH` is a name you use for something else, point it at a new name before you analyze.

`python -m edl falkor examples/mini-repo` does only this publish, and prints the chip text. Use it to test the connection without opening the page.

Tests never open the network. `publish` returns immediately when pytest is running.

## How this project connects

Port 6379 is not open from a laptop to `13.236.193.238`. The website on port 3000 is on the same machine as the database, so the app logs in there and asks the website to use `localhost:6379`.

You only put the password in `FALKORDB_PASSWORD`. The username stays empty. The app sends that password to `http://13.236.193.238:3000/api/auth/tokens/credentials` and then writes Cypher through `/api/graph/<name>`. The password is not printed.

On 15 October 2026 that write succeeded. Graph `Test` has the offline example: 11 nodes, 12 edges, and the services `checkout`, `payments`, and `search`.

To see it, open [http://13.236.193.238:3000/login](http://13.236.193.238:3000/login) and connect with:

```text
falkor://:<password>@localhost:6379
```

Then open the graph named `Test`.

## What is not done inside FalkorDB yet

The six review tools still run against the in-memory graph. FalkorDB receives a copy after the scan. The next step is to make those six tools run as fixed Cypher through this same browser login, still with no free-text query from the page. The state must stay in `edl/policy.py` either way.
