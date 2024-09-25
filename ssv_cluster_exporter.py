import argparse
import asyncio
import copy
import enum
import json
import logging
import pathlib
import sys
import typing

from aiohttp import client, web
import furl  # type: ignore[import-untyped]
from prometheus_async import aio
from prometheus_client import Gauge
from pydantic import AfterValidator, BaseModel, computed_field
from pydantic_settings import BaseSettings
import yaml
from web3 import Web3
from web3.contract import AsyncContract
from web3.eth import AsyncEth
from web3.providers.rpc import AsyncHTTPProvider

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
        if self is self.MAINNET:
            return "0xafE830B6Ee262ba11cce5F32fDCd760FFE6a66e4"
        elif self is self.HOLESKY:
            return "0x352A18AEe90cdcd825d1E37d9939dCA86C00e281"
        else:
            raise RuntimeError(
                "Do not know SSV network views contract for this network"
            )


# ###################
# Aiohttp & web3 apps
def get_application() -> web.Application:
    app = web.Application()
    app.router.add_get("/metrics", aio.web.server_stats)
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
    match network:
        case SupportedNetworks.HOLESKY:
            address = "0x38A4794cCEd47d3baf7370CcC43B560D3a1beEFA"
        case SupportedNetworks.MAINNET:
            address = "0xafE830B6Ee262ba11cce5F32fDCd760FFE6a66e4"
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


# #############
# Command line
logger = logging.getLogger(__name__)
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
    pass


SSVNetworkViewsCallArgs = typing.Tuple[
    str, tuple[int, ...], tuple[int, int, int, bool, int]
]


class SSVClusterContract(BaseModel):
    """A facade for web3 contract data retrieval."""

    network_views: AsyncContract
    clusters: set[SSVCluster]

    class Config:
        arbitrary_types_allowed = True

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
            futs.append(asyncio.create_task(self.get_cluster_balance(cluster)))
        await asyncio.gather(*futs)

    async def fetch_burn_rates(self) -> None:
        futs = []
        for cluster in self.clusters:
            futs.append(asyncio.create_task(self.get_cluster_burn_rate(cluster)))
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

    session: client.ClientSession

    @computed_field  # type: ignore
    @property
    def network_views(self) -> AsyncContract:
        return get_ssv_network_views_contract(self.ethereum_rpc, self.network)  # type: ignore[arg-type]

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
                asyncio.create_task(self.get_owner_clusters(owner_config.address))
            )
        for cluster_config in self.clusters:
            futs.append(
                asyncio.create_task(self.get_cluster_by_id(cluster_config.cluster_id))
            )

        responses = await asyncio.gather(*futs)
        for response in responses:
            clusters += response

        return clusters

    def update_metrics(self, *clusters: SSVCluster) -> None:
        """Update metrics for Prometheus consumption."""
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

    async def tick(self) -> None:
        """Perform single data retrieval and metrics update."""
        try:
            clusters = set(await self.fetch_clusters_info())
            latest_metric_fetcher = SSVClusterContract(
                network_views=self.network_views, clusters=clusters
            )
            await latest_metric_fetcher.fetch_all()
            self.update_metrics(*clusters)
        except Exception:
            logger.exception("Failed to update cluster details")

    async def loop(self) -> None:
        """Infinite loop that spawns checker tasks."""
        while True:
            asyncio.ensure_future(self.tick())
            await self.sleep()
        self.stopped.set()


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
        config_data["session"] = client.ClientSession(loop=loop)
        exporter = SSVClusterExporter(**config_data)
    except yaml.error.YAMLError:
        logger.exception("Invalid config YAML")
        exit(2)
    except ValueError:
        logger.exception("Invalid config data")
        exit(2)
    else:
        app = get_application()
        loop.create_task(exporter.loop())
        web.run_app(app, host=args.host, port=args.port, loop=loop)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
    )
    main()
