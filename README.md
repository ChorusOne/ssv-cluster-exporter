ssv-cluster-exporter
====================

Prometheus exporter for SSV cluster metrics.

Available metrics
-----------------

`ssv_cluster_balance` -- current balance of SSV tokens for given cluster

Dimensions:
 - `cluster_id` an unique 0x-prefixed hex identifier of the cluster in SSV system
 - `id` integer identifier of the cluster in SSV system
 - `owner` 0x-prefixed 40 letters Ethereum address value
 - `network` one of `mainnet`, `holesky`
 - `state` one of `active`, `inactive`, `liquidated`
 - `operators` comma separated list of cluster operators integer identifiers in SSV system

----------

`ssv_cluster_network_fee_index` -- current [network fee index](https://docs.ssv.network/learn/protocol-overview/tokenomics/payments) of a cluster in SSV system

Dimensions:
 - `cluster_id` an unique 0x-prefixed hex identifier of the cluster in SSV system
 - `id` integer identifier of the cluster in SSV system
 - `owner` 0x-prefixed 40 letters Ethereum address value
 - `network` one of `mainnet`, `holesky`
 - `state` one of `active`, `inactive`, `liquidated`
 - `operators` comma separated list of cluster operators integer identifiers in SSV system

----------

`ssv_cluster_validators_count` -- number of validators loaded into SSV cluster

Dimensions:
 - `cluster_id` an unique 0x-prefixed hex identifier of the cluster in SSV system
 - `id` integer identifier of the cluster in SSV system
 - `owner` 0x-prefixed 40 letters Ethereum address value
 - `network` one of `mainnet`, `holesky`
 - `state` one of `active`, `inactive`, `liquidated`
 - `operators` comma separated list of cluster operators integer identifiers in SSV system



Installation
------------
This program uses [Pipenv](https://pipenv.pypa.io/en/latest/) to manage
dependencies. It have been tested with Python 3.12

To create dedicated virtual environment and install dependencies, after
cloning an application, navigate to its root folder and invoke

```bash
pipenv sync
```

Configuration
-------------
Exporter accepts single positional parameter `config_file` which is a location
of YAML config file. The YAML file accepts following parameters:

- `interval_ms` -- interval in milliseconds of checking SSV API
- `clusters` -- list of clusters, every cluster should have `network` and `cluster_id` properties
- `owners` -- list of owner addresses, every owner should have `network` and `address` properties

- `network` property should be one of `mainnet` or `holesky`
- `cluster_id` is an unique 0x-prefixed hex identifier of the cluster in SSV system
- `address` is 0x-prefixed 40 letters Ethereum address value


See [Config example file](./config.example.yml) for full-featured example

Running
--------

After creating config file, run application like

```bash
pipenv run python3 ssv_cluster_exporter </path/to/config/file>
```

By default, metrics will be available via http://127.0.0.1:29339/metrics URL.

To change host and port for Prometheus metrics server, use following parameters

```
  -H HOST, --host HOST  Listen on this host.
  -P PORT, --port PORT  Listen on this port.
```
