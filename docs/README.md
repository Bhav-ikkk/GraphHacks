# Syzygy EDL — read this first

These pages are the working explanation of the prototype. They describe the code that is in the repository now.

Read them in this order:

1. [How a review moves through the system](01-dataflow.md) — start here. One change, from the URL you paste to the state on the screen.
2. [What a repository actually provides](02-repository-facts.md) — which facts are read from git, and which facts are left missing on purpose.
3. [How a state is chosen](03-decisions.md) — the five states, the rules, the three evidence modes, and the issue button.
4. [FalkorDB](04-falkordb.md) — how the Python client matches the FalkorDB docs, what `.env` is for, and why the browser on port 3000 is not the database.
5. [What is built, and what the team still has to add](05-for-the-team.md) — so you can split the remaining work.

The product does not load a fictional shop, and it does not invent owners, incidents, test results, or deployment health. You choose a repository. The example folder `examples/mini-repo` is only a small tree you can point at while you are offline.
