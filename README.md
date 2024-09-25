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

`ssv_cluster_burn_rate` -- current [burn rate](https://docs.ssv.network/learn/protocol-overview/tokenomics/liquidations#burn-rate) of a cluster in SSV system

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

----------

`ssv_network_fee` -- current SSV [network fee](https://docs.ssv.network/learn/protocol-overview/tokenomics/fees#k4tw9to38r3v)

Dimensions:
 - `network` one of `mainnet`, `holesky`

----------

`ssv_minimum_liquidation_collateral` -- current SSV [minimum liquidation collateral](https://docs.ssv.network/learn/protocol-overview/tokenomics/liquidations#minimum-liquidation-collateral) 

Dimensions:
 - `network` one of `mainnet`, `holesky`


----------

`ssv_liquidation_threshold_period` -- current SSV [liquidation threshold period](https://docs.ssv.network/learn/protocol-overview/tokenomics/liquidations#liquidation-threshold-period)

Dimensions:
 - `network` one of `mainnet`, `holesky`



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
- `ethereum_rpc` -- address of Ethereum JSON-RPC endpoint to use for calling contracts
- `network` -- this should be either `mainnet` or `holesky`
- `clusters` -- list of clusters, every cluster should have `cluster_id` properties
- `owners` -- list of owner addresses, every owner should have `address` properties

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
