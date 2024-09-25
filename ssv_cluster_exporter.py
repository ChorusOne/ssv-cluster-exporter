import argparse
import asyncio
import copy
import enum
import functools
import json
import logging
import pathlib
import sys
import typing

from aiohttp import client, web
import furl  # type: ignore[import-untyped]
from prometheus_async import aio
from prometheus_client import Gauge
from pydantic import AfterValidator, BaseModel, ConfigDict, computed_field
from pydantic_settings import BaseSettings
from web3 import Web3
from web3.contract import AsyncContract
from web3.eth import AsyncEth
from web3.providers.rpc import AsyncHTTPProvider
import yaml

logger = logging.getLogger(__name__)


# ############################
# Supported Ethereum networks
class SupportedNetworks(str, enum.Enum):
    MAINNET = "mainnet"
    HOLESKY = "holesky"

    def __str__(self) -> str:
        return self.value

    def ssv_network_views_contract(self) -> str:
        # See https://docs.ssv.network/developers/smart-contracts
        match self.value:
            case SupportedNetworks.HOLESKY:
                return "0x38A4794cCEd47d3baf7370CcC43B560D3a1beEFA"
            case SupportedNetworks.MAINNET:
                return "0xafE830B6Ee262ba11cce5F32fDCd760FFE6a66e4"
        raise RuntimeError("Can not derive SSV network views address for network")


# ####################
# Aiohttp & web3 apps
async def start_exporter_app(app: web.Application) -> None:
    exporter: SSVClusterExporter = app[exporter_app_key]
    # Reuse client session for web3 and ssv api
    await exporter.ethereum_rpc.provider.cache_async_session(exporter.session)  # type: ignore[attr-defined]
    # Acquire data once to verify its working
    await exporter.tick()
    # Spawn long-running process
    exporter.start()


async def stop_exporter_app(app: web.Application) -> None:
    exporter: SSVClusterExporter = app[exporter_app_key]
    await exporter.stop()


def get_application(exporter: "SSVClusterExporter") -> web.Application:
    app = web.Application()
    app[exporter_app_key] = exporter
    app.router.add_get("/metrics", aio.web.server_stats)
    app.on_startup.append(start_exporter_app)
    app.on_shutdown.append(stop_exporter_app)
    return app


def get_web3_provider(ethereum_rpc_url: str) -> Web3:
    return Web3(
        provider=AsyncHTTPProvider(ethereum_rpc_url),  # type: ignore[arg-type]
        modules={"eth": (AsyncEth,)},
    )


def get_ssv_network_views_contract_abi() -> typing.Any:
    with open(
        pathlib.Path(__file__).parent / "contract/SSVNetworkViews.json", "r"
    ) as fl:
        return json.loads(fl.read())


def get_ssv_network_views_contract(
    web3: Web3, network: SupportedNetworks
) -> AsyncContract:
    abi = get_ssv_network_views_contract_abi()
    address = network.ssv_network_views_contract()
    contract: AsyncContract = web3.eth.contract(address=address, abi=abi)  # type: ignore[call-overload]
    return contract


Web3RpcClient = typing.Annotated[str, AfterValidator(get_web3_provider)]


# ########
# Metrics
ssv_cluster_balance = Gauge(
    name="ssv_cluster_balance",
    documentation="Current balance for SSV cluster",
    labelnames=["cluster_id", "id", "owner", "network", "state", "operators"],
)
ssv_cluster_burn_rate = Gauge(
    name="ssv_cluster_burn_rate",
    documentation="SSV cluster burn rate",
    labelnames=["cluster_id", "id", "owner", "network", "state", "operators"],
)
ssv_cluster_validators_count = Gauge(
    name="ssv_cluster_validators_count",
    documentation="Number of validators in the SSV cluster",
    labelnames=["cluster_id", "id", "owner", "network", "state", "operators"],
)


ssv_network_fee = Gauge(
    name="ssv_network_fee",
    documentation="Current SSV network fee in SSV tokens",
    labelnames=["network"],
)
ssv_minimum_liquidation_collateral = Gauge(
    name="ssv_minimum_liquidation_collateral",
    documentation="Current minimum liquidation collateral for SSV network",
    labelnames=["network"],
)
ssv_liquidation_threshold_period = Gauge(
    name="ssv_liquidation_threshold_period",
    documentation="SSV liquidation threshold period, number of blocks that should be always funded",
    labelnames=["network"],
)


# #############
# Command line
arg_parser = argparse.ArgumentParser("SSV Cluster Data Exporter for Prometheus.")
arg_parser.add_argument(
    "config_file",
    default="config.yml",
    help="Location of a config file.",
    type=pathlib.Path,
)
arg_parser.add_argument(
    "-H", "--host", default="127.0.0.1", help="Listen on this host."
)
arg_parser.add_argument(
    "-P", "--port", type=int, default=29339, help="Listen on this port."
)


# ###################
# Settings and logic
class ClusterConfig(BaseModel):
    """Cluster config for data retrieval."""

    cluster_id: str


class OwnerConfig(BaseModel):
    """Owner config for data retrieval."""

    address: str


class SSVCluster(BaseModel):
    """Represents single SSV cluster retrieved over API."""

    id: int
    clusterId: str
    network: SupportedNetworks
    ownerAddress: str
    validatorCount: int
    networkFeeIndex: str
    index: str
    balance: str
    active: bool
    isLiquidated: bool
    operators: list[int]

    # Values received from the contract
    latest_balance: int | None = None
    latest_burn_rate: int | None = None

    def current_balance(self) -> int:
        return int(self.balance)

    def current_network_fee(self) -> int:
        return int(self.networkFeeIndex)

    def cluster_state(self) -> str:
        if not self.active and not self.isLiquidated:
            return "inactive"
        elif self.active and not self.isLiquidated:
            return "active"
        elif self.isLiquidated:
            return "liquidated"
        else:
            return "unknown"

    def operators_label(self) -> str:
        return ",".join(map(str, self.operators))

    def __hash__(self) -> int:
        """Deduplicate clusters by ID"""
        return self.id


class SSVAPIError(Exception):
    """Error when communicating with SSV API."""

    pass


# Common call arguments for cluster methods in SSVNetworkViews contract
SSVNetworkViewsCallArgs = typing.Tuple[
    str, tuple[int, ...], tuple[int, int, int, bool, int]
]


class SSVNetworkProperties(BaseModel):
    """Network-wide tokenomics properties."""

    network_fee: int
    minimum_liquidation_collateral: int
    liquidation_threshold_period: int


class SSVNetworkContract(BaseModel):
    """A facade for web3 contract data retrieval for network wide values."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    network_views: AsyncContract

    async def fetch_network_fee(self) -> int:
        return int(await self.network_views.functions.getNetworkFee().call())

    async def fetch_minimum_liquidation_collateral(self) -> int:
        return int(
            await self.network_views.functions.getMinimumLiquidationCollateral().call()
        )

    async def fetch_liquidation_threshold_period(self) -> int:
        return int(
            await self.network_views.functions.getLiquidationThresholdPeriod().call()
        )

    async def fetch_all(self) -> SSVNetworkProperties:
        (n_f, m_l_c, l_t_p) = await asyncio.gather(
            self.fetch_network_fee(),
            self.fetch_minimum_liquidation_collateral(),
            self.fetch_liquidation_threshold_period(),
        )
        return SSVNetworkProperties(
            network_fee=n_f,
            minimum_liquidation_collateral=m_l_c,
            liquidation_threshold_period=l_t_p,
        )


class SSVClusterContract(BaseModel):
    """A facade for web3 contract data retrieval for clusters."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    network_views: AsyncContract
    clusters: set[SSVCluster]
    loop: asyncio.AbstractEventLoop

    def contract_call_args(self, cluster: SSVCluster) -> SSVNetworkViewsCallArgs:
        return (
            cluster.ownerAddress,
            tuple(cluster.operators),
            (
                cluster.validatorCount,
                int(cluster.networkFeeIndex),
                int(cluster.index),
                cluster.active,
                cluster.current_balance(),
            ),
        )

    async def get_cluster_balance(self, cluster: SSVCluster) -> None:
        value = await self.network_views.functions.getBalance(
            *self.contract_call_args(cluster)
        ).call()
        cluster.latest_balance = value

    async def get_cluster_burn_rate(self, cluster: SSVCluster) -> None:
        value = await self.network_views.functions.getBurnRate(
            *self.contract_call_args(cluster)
        ).call()
        cluster.latest_burn_rate = value

    async def fetch_balances(self) -> None:
        futs = []
        for cluster in self.clusters:
            futs.append(self.loop.create_task(self.get_cluster_balance(cluster)))
        await asyncio.gather(*futs)

    async def fetch_burn_rates(self) -> None:
        futs = []
        for cluster in self.clusters:
            futs.append(self.loop.create_task(self.get_cluster_burn_rate(cluster)))
        await asyncio.gather(*futs)

    async def fetch_all(self) -> None:
        futs = [
            self.fetch_balances(),
            self.fetch_burn_rates(),
        ]
        await asyncio.gather(*futs)


class SSVClusterExporter(BaseSettings):
    """Represents configured exporter and defines metrics update logic."""

    interval_ms: int = 60000
    clusters: list[ClusterConfig]
    owners: list[OwnerConfig]
    network: SupportedNetworks
    ethereum_rpc: Web3RpcClient
    base_ssv_url: furl.furl = furl.furl("https://api.ssv.network/api/v4/")

    loop: asyncio.AbstractEventLoop

    # Stopping
    stopping: bool = False
    stopped: asyncio.Event = asyncio.Event()

    @computed_field  # type: ignore
    @functools.cached_property
    def network_views(self) -> AsyncContract:
        return get_ssv_network_views_contract(self.ethereum_rpc, self.network)  # type: ignore[arg-type]

    @computed_field  # type: ignore
    @functools.cached_property
    def session(self) -> client.ClientSession:
        return client.ClientSession(loop=self.loop)

    def on_runner_task_done(self, *args: typing.Any) -> None:
        self.stopped.set()

    def start(self) -> None:
        self._runner_task = self.loop.create_task(self.run())
        # Raise event when task is stopped
        self._runner_task.add_done_callback(self.on_runner_task_done)

    async def stop(self) -> None:
        logger.info("Gracefully shutting down application")
        self.stopping = True
        self._runner_task.cancel()
        if not self.stopped.is_set():
            await self.stopped.wait()
        await self.session.close()
        logger.info("Stopped components, will exit")

    async def sleep(self) -> None:
        await asyncio.sleep(self.interval_ms / 1000)

    async def request(self, uri: str, **params: str) -> typing.Any:
        """Perform request to SSV API server and handle all kinds of errors."""
        url = copy.deepcopy(self.base_ssv_url).join(uri)
        url.args.update(params)
        logger.info("Requesting SSV API Url: %s", url)

        try:
            response = await self.session.get(
                url=str(url),
                headers={
                    "User-Agent": "ssv-cluster-exporter.py",
                    "Accept": "application/json",
                },
            )
        except (client.ClientError, OSError):
            logger.exception("Failed requesting SSV API Url %s", url)
            raise SSVAPIError("Client HTTP interaction error")
        else:
            if response.status != 200:
                raise SSVAPIError("Non-200 SSV API response code: %s", response.status)
            try:
                response_data = await response.json()
            except (client.ClientError, OSError):
                logger.exception("Failed retrieving response data from SSV API")
                raise SSVAPIError("Client data reading error")
            else:
                return response_data

    async def get_cluster_by_id(self, cluster_id: str) -> list[SSVCluster]:
        """Given the previously known cluster id, retrieve information."""
        clusters = []
        logger.info("Checking cluster %s", cluster_id)
        try:
            response_json = await self.request(f"{self.network}/clusters/{cluster_id}")
        except SSVAPIError:
            logger.exception("Failed to retrieve information for cluster %s:")
        else:
            if response_json["data"]:
                clusters.append(SSVCluster(**response_json["data"]))
            else:
                logger.warning("No data recorded for cluster %s", cluster_id)
        return clusters

    async def get_owner_clusters(self, owner: str, page: int = 1) -> list[SSVCluster]:
        """Dynamically discover SSV clusters for given owner address."""
        clusters = []
        logger.info("Checking owner %s", owner)
        try:
            response_json = await self.request(
                f"{self.network}/clusters/owner/{owner}", page=str(page)
            )
        except SSVAPIError:
            logger.exception(
                "Failed to retrieve information about clusters for owner %s", owner
            )
        else:
            for cluster_data in response_json["clusters"]:
                clusters.append(SSVCluster(**cluster_data))

            num_pages = response_json["pagination"]["pages"]
            # Handle pagination once, avoiding higher recursion levels
            if page == 1 and page < num_pages:
                while page < num_pages:
                    page += 1
                    next_page_clusters = await self.get_owner_clusters(owner, page)
                    clusters += next_page_clusters

        return clusters

    async def fetch_clusters_info(self) -> list[SSVCluster]:
        """Given the clusters and owners lists, finds all the SSV clusters for them."""
        futs = []
        clusters = []

        for owner_config in self.owners:
            futs.append(
                self.loop.create_task(self.get_owner_clusters(owner_config.address))
            )
        for cluster_config in self.clusters:
            futs.append(
                self.loop.create_task(self.get_cluster_by_id(cluster_config.cluster_id))
            )

        responses = await asyncio.gather(*futs)
        for response in responses:
            clusters += response

        return clusters

    def update_clusters_metrics(self, *clusters: SSVCluster) -> None:
        """Update cluster related metrics for Prometheus consumption."""
        for cluster in clusters:
            labels = [
                cluster.clusterId,
                cluster.id,
                cluster.ownerAddress,
                cluster.network,
                cluster.cluster_state(),
                cluster.operators_label(),
            ]
            ssv_cluster_balance.labels(*labels).set(float(cluster.latest_balance or 0))
            ssv_cluster_burn_rate.labels(*labels).set(
                float(cluster.latest_burn_rate or 0)
            )
            ssv_cluster_validators_count.labels(*labels).set(cluster.validatorCount)

    def update_network_metrics(self, network_properties: SSVNetworkProperties) -> None:
        """Update network-wide metrics with value retrieven from contract."""
        ssv_network_fee.labels(self.network).set(network_properties.network_fee)
        ssv_liquidation_threshold_period.labels(self.network).set(
            network_properties.liquidation_threshold_period
        )
        ssv_minimum_liquidation_collateral.labels(self.network).set(
            network_properties.minimum_liquidation_collateral
        )

    async def clusters_updates(self) -> None:
        """Run cluster-specific metrics update."""
        clusters = set(await self.fetch_clusters_info())
        latest_metric_fetcher = SSVClusterContract(
            network_views=self.network_views,
            clusters=clusters,
            loop=self.loop,
        )
        await latest_metric_fetcher.fetch_all()
        self.update_clusters_metrics(*clusters)

    async def network_updates(self) -> None:
        """Run network-wide metrics update."""
        network_metric_fetcher = SSVNetworkContract(network_views=self.network_views)
        network_properties = await network_metric_fetcher.fetch_all()
        self.update_network_metrics(network_properties)

    async def tick(self) -> None:
        """Perform single data retrieval and metrics update."""
        try:
            await asyncio.gather(
                self.clusters_updates(),
                self.network_updates(),
            )
        except Exception:
            logger.exception("Failed to update cluster details")
        if self.stopping:
            await self.session.close()

    async def run(self) -> None:
        """Infinite loop that spawns checker tasks."""
        while not self.stopping:
            self.loop.create_task(self.tick())
            await self.sleep()
        self.stopped.set()


# Aiohttp app key for exporter component
exporter_app_key: web.AppKey[SSVClusterExporter] = web.AppKey(
    "exporter", SSVClusterExporter
)


# #############
# Entry point
def main() -> None:
    args = arg_parser.parse_args()

    if not args.config_file.exists():
        logger.error("Config file does not exist at %s", args.config_file)
        exit(2)
    config_text = args.config_file.read_text()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        config_data = yaml.safe_load(config_text)
        config_data["loop"] = loop
        exporter = SSVClusterExporter(**config_data)
    except yaml.error.YAMLError:
        logger.exception("Invalid config YAML")
        exit(2)
    except ValueError:
        logger.exception("Invalid config data")
        exit(2)
    else:
        app = get_application(exporter)
        web.run_app(
            app, host=args.host, port=args.port, loop=loop, handler_cancellation=True
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
    )
    main()
