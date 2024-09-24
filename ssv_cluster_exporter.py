import argparse
import asyncio
import copy
import enum
import logging
import pathlib
import sys
import typing
import urllib.parse

from aiohttp import client, web
import furl
from prometheus_async import aio
from prometheus_client import Gauge
from pydantic import BaseModel
from pydantic_settings import BaseSettings
import yaml


logger = logging.getLogger(__name__)


# ########
# Metrics
ssv_cluster_balance = Gauge(
    name="ssv_cluster_balance",
    documentation="Current balance for SSV cluster",
    labelnames=["cluster_id", "id", "owner", "network", "state", "operators"],
)
ssv_cluster_network_fee = Gauge(
    name="ssv_cluster_network_fee_index",
    documentation="SSV latest network fee index as reported for given cluster",
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
    nargs="?",
    default="config.yml",
    help="Location of a config file.",
    type=pathlib.Path,
)
arg_parser.add_argument(
    "-H", "--host", default="127.0.0.1", help="Listen on this host."
)
arg_parser.add_argument(
    "-P", "--port", type=str, default=29339, help="Listen on this port."
)


# ###################
# Settings and logic
class SupportedNetworks(str, enum.Enum):
    MAINNET = "mainnet"
    HOLESKY = "holesky"

    def __str__(self):
        return self.value


class ClusterConfig(BaseModel):
    """Cluster config for data retrieval."""

    cluster_id: str
    network: SupportedNetworks


class OwnerConfig(BaseModel):
    """Owner config for data retrieval."""

    address: str
    network: SupportedNetworks


class SSVCluster(BaseModel):
    """Represents single SSV cluster retrieved over API."""

    id: int
    clusterId: str
    network: SupportedNetworks
    ownerAddress: str
    validatorCount: int
    networkFeeIndex: str
    balance: str
    active: bool
    isLiquidated: bool
    operators: list[int]

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


class SSVAPIError(Exception):
    pass


class SSVClusterExporter(BaseSettings):
    """Represents configured exporter and defines metrics update logic."""

    interval_ms: int = 60000
    clusters: list[ClusterConfig]
    owners: list[OwnerConfig]
    base_url: furl.furl = furl.furl("https://api.ssv.network/api/v4/")
    session: client.ClientSession
    stopping: bool = False
    stopped: asyncio.Event = asyncio.Event()

    async def sleep(self):
        await asyncio.sleep(self.interval_ms / 1000)

    async def request(self, uri, **params) -> dict[typing.Any]:
        """Perform request to SSV API server and handle all kinds of errors."""
        url = copy.deepcopy(self.base_url).join(uri)
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
        except (client.ClientError, OSError) as exc:
            logger.exception("Failed requesting SSV API Url %s", url)
            raise SSVAPIError("Client HTTP interaction error")
        else:
            if response.status != 200:
                raise SSVAPIError("Non-200 SSV API response code: %s", response.status)
            try:
                response_data = await response.json()
            except (client.ClientError, OSError) as exc:
                logger.exception("Failed retrieving response data from SSV API")
                raise SSVAPIError("Client data reading error")
            else:
                return response_data

    async def get_cluster_by_id(
        self, network: SupportedNetworks, cluster_id: str
    ) -> list[SSVCluster]:
        """Given the previously known cluster id, retrieve information."""
        clusters = []
        logger.info("Checking cluster %s", cluster_id)
        try:
            response_json = await self.request(f"{network}/clusters/{cluster_id}")
        except SSVAPIError:
            logger.exception("Failed to retrieve information for cluster %s:")
        else:
            if response_json["data"]:
                clusters.append(SSVCluster(**response_json["data"]))
            else:
                logger.warning("No data recorded for cluster %s", cluster_id)
        return clusters

    async def get_owner_clusters(
        self, network: SupportedNetworks, owner: str, page: int = 1
    ) -> list[SSVCluster]:
        """Dynamically discover SSV clusters for given owner address."""
        clusters = []
        logger.info("Checking owner %s", owner)
        try:
            response_json = await self.request(f"{network}/clusters/owner/{owner}")
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
                    next_page_clusters = await self.get_owner_clusters(
                        network, owner, page
                    )
                    clusters += next_page_clusters

        return clusters

    async def clusters_info(self) -> list[SSVCluster]:
        """Given the clusters and owners lists, finds all the SSV clusters for them."""
        futs = []
        clusters = []

        for owner_config in self.owners:
            futs.append(
                asyncio.create_task(
                    self.get_owner_clusters(owner_config.network, owner_config.address)
                )
            )
        for cluster_config in self.clusters:
            futs.append(
                asyncio.create_task(
                    self.get_cluster_by_id(
                        cluster_config.network, cluster_config.cluster_id
                    )
                )
            )

        responses = await asyncio.gather(*futs)
        for response in responses:
            clusters += response

        return clusters

    def update_metrics(self, *clusters: list[SSVCluster]):
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
            ssv_cluster_balance.labels(*labels).set(cluster.current_balance())
            ssv_cluster_network_fee.labels(*labels).set(cluster.current_network_fee())
            ssv_cluster_validators_count.labels(*labels).set(cluster.validatorCount)

    async def tick(self):
        """Perform single data retrieval and metrics update."""
        try:
            clusters = await self.clusters_info()
            self.update_metrics(*clusters)
        except Exception:
            logger.exception("Failed to update cluster details")

    async def loop(self):
        """Infinite loop that spawns checker tasks."""
        while not self.stopping:
            asyncio.ensure_future(self.tick())
            await self.sleep()
        self.stopped.set()


# ############
# Aiohttp app
def get_application() -> web.Application:
    app = web.Application()
    app.router.add_get("/metrics", aio.web.server_stats)
    return app


# #############
# Entry point
def main():
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
