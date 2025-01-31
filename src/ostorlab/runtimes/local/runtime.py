"""Local runtime runs agents locally.

The local runtime requires Docker Swarm to run robust long-running services with a set of configured services, like
a local RabbitMQ.
"""
import logging
import socket
from typing import List
from typing import Optional

import click
import docker
import requests
import tenacity
from docker.models import services as docker_models_services

from ostorlab import exceptions
from ostorlab.assets import asset as base_asset
from ostorlab.cli import console as cli_console
from ostorlab.cli import docker_requirements_checker
from ostorlab.cli import install_agent
from ostorlab.runtimes import definitions
from ostorlab.runtimes import runtime
from ostorlab.runtimes.local import agent_runtime
from ostorlab.runtimes.local import log_streamer
from ostorlab.runtimes.local.models import models
from ostorlab.runtimes.local.services import mq

NETWORK_PREFIX = 'ostorlab_local_network'

logger = logging.getLogger(__name__)
console = cli_console.Console()

ASSET_INJECTION_AGENT_DEFAULT = 'agent/ostorlab/inject_asset'
TRACKER_AGENT_DEFAULT = 'agent/ostorlab/tracker'
LOCAL_PERSIST_VULNZ_AGENT_DEFAULT = 'agent/ostorlab/local_persist_vulnz'

DEFAULT_AGENTS = [
    ASSET_INJECTION_AGENT_DEFAULT,
    TRACKER_AGENT_DEFAULT,
    LOCAL_PERSIST_VULNZ_AGENT_DEFAULT
]


class UnhealthyService(exceptions.OstorlabError):
    """A service by the runtime is considered unhealthy."""


class AgentNotInstalled(exceptions.OstorlabError):
    """Agent image not installed."""


class AgentNotHealthy(exceptions.OstorlabError):
    """Agent not healthy."""


def _has_container_image(agent: definitions.AgentSettings):
    """Check if container image is available"""
    return agent.container_image is not None


def _is_agent_status_ok(ip: str) -> bool:
    """Agent are expected to expose a healthcheck service on port 5000 that returns status code 200 and `OK` response.

    Args:
        ip: The agent IP address.

    Returns:
        True if the agent is healthy, false otherwise.
    """
    status_ok = False
    try:
        status_ok = requests.get(f'http://{ip}:5000/status').text == 'OK'
    except requests.exceptions.ConnectionError:
        logger.error('unable to connect to %s', ip)
    return status_ok


def _get_task_ips(service: docker_models_services.Service) -> List[str]:
    """Returns list of IP addresses assigned to the tasks of a docker service.

    Args:
        service: docker service.

    Returns:
        List of IP addresses.
    """
    # current implementation supports only one task per service.
    logger.info('getting ips for task %s', service.name)
    try:
        ips = socket.gethostbyname_ex(f'tasks.{service.name}')
        logger.info('found ips %s for task %s', ips, service.name)
        return ips[2]
    except socket.gaierror:
        return []


def _is_service_type_run(service: docker_models_services.Service) -> bool:
    """Checks if the service should run once or should be continuously running.

    Args:
        service: Docker service.

    Returns:
        Bool indicating if the service is run-once or long-running.
    """
    return service.attrs['Spec']['TaskTemplate']['RestartPolicy']['Condition'] == 'none'


class LocalRuntime(runtime.Runtime):
    """Local runtime runes agents locally using Docker Swarm.
    Local runtime starts a Vanilla RabbitMQ service, starts all the agents listed in the `AgentRunDefinition`, checks
    their status and then inject the target asset.
    """

    def __init__(self) -> None:
        super().__init__()
        self.follow = []
        self._mq_service: Optional[mq.LocalRabbitMQ] = None
        self._log_streamer = log_streamer.LogStream()

        if not docker_requirements_checker.is_docker_installed():
            console.error('Docker is not installed.')
            raise click.exceptions.Exit(2)
        elif not docker_requirements_checker.is_user_permitted():
            console.error('User does not have permissions to run docker.')
            raise click.exceptions.Exit(2)
        elif not docker_requirements_checker.is_docker_working():
            console.error('Error using docker.')
            raise click.exceptions.Exit(2)
        else:
            if not docker_requirements_checker.is_swarm_initialized():
                docker_requirements_checker.init_swarm()
            self._docker_client = docker.from_env()

        self._scan_db: Optional[models.Scan] = None

    @property
    def name(self) -> str:
        """Local runtime instance name."""
        if self._scan_db is not None:
            return str(self._scan_db.id)
        else:
            raise ValueError('Scan not created yet')

    @property
    def network(self) -> str:
        return f'{NETWORK_PREFIX}_{self.name}'

    def can_run(self, agent_group_definition: definitions.AgentGroupDefinition) -> bool:
        """Checks if the runtime can run the provided agent run definition.

        Args:
            agent_group_definition: Agent and Agent group definition.

        Returns:
            Always true for the moment as the local runtime doesn't have restrictions on what it can run.
        """
        del agent_group_definition
        return True

    def scan(self, title: str, agent_group_definition: definitions.AgentGroupDefinition,
             asset: base_asset.Asset) -> None:
        """Start scan on asset using the provided agent run definition.

        The scan takes care of starting all the scan required services, ensuring they are healthy, starting all the
         agents, ensuring they are healthy and then injects the target asset.

        Args:
            title: Scan title
            agent_group_definition: Agent run definition defines the set of agents and how agents are configured.
            asset: the target asset to scan.

        Returns:
            None
        """
        try:
            console.info('Creating scan entry')
            self._scan_db = self._create_scan_db(asset=str(asset), title=title)
            console.info('Creating network')
            self._create_network()
            console.info('Starting services')
            self._start_services()
            console.info('Checking services are healthy')
            self._check_services_healthy()

            console.info('Starting pre-agents')
            self._start_pre_agents()
            console.info('Checking pre-agents are healthy')
            is_healthy = self._check_agents_healthy()
            if is_healthy is False:
                raise AgentNotHealthy()

            console.info('Starting agents')
            self._start_agents(agent_group_definition)
            console.info('Checking agents are healthy')
            is_healthy = self._check_agents_healthy()
            if is_healthy is False:
                raise AgentNotHealthy()

            console.info('Starting post-agents')
            self._start_post_agents()
            console.info('Checking post-agents are healthy')
            is_healthy = self._check_agents_healthy()
            if is_healthy is False:
                raise AgentNotHealthy()

            console.info('Injecting asset')
            self._inject_asset(asset)
            console.info('Updating scan status')
            self._update_scan_progress('IN_PROGRESS')
            console.success('Scan created successfully')

        except AgentNotHealthy:
            console.error('Agent not starting')
            self.stop(self._scan_db.id)
            self._update_scan_progress('ERROR')
        except AgentNotInstalled as e:
            console.error(f'Agent {e} not installed')

    def stop(self, scan_id: str) -> None:
        """Remove a service (scan) belonging to universe with scan_id(Universe Id).

        Args:
            scan_id: The id of the scan to stop.
        """

        stopped_services = []
        stopped_network = []
        stopped_configs = []
        client = docker.from_env()
        services = client.services.list()
        for service in services:
            service_labels = service.attrs['Spec']['Labels']
            logger.debug('comparing %s and %s', service_labels.get('ostorlab.universe'), scan_id)
            if service_labels.get('ostorlab.universe') == scan_id:
                stopped_services.append(service)
                service.remove()

        networks = client.networks.list()
        for network in networks:
            network_labels = network.attrs['Labels']
            if network_labels is not None and network_labels.get('ostorlab.universe') == scan_id:
                logger.debug('removing network %s', network_labels)
                stopped_network.append(network)
                network.remove()

        configs = client.configs.list()
        for config in configs:
            config_labels = config.attrs['Spec']['Labels']
            if config_labels.get('ostorlab.universe') == scan_id:
                logger.debug('removing config %s', config_labels)
                stopped_configs.append(config)
                config.remove()

        if stopped_services or stopped_network or stopped_configs:
            console.success('All scan components stopped.')

        database = models.Database()
        session = database.session
        scan = session.query(models.Scan).get(int(scan_id))
        if scan:
            scan.progress = 'STOPPED'
            session.commit()
        console.success('Scan stopped successfully.')

    def _create_scan_db(self, title: str, asset: str):
        """Persist the scan in the database"""
        models.Database().create_db_tables()
        return models.Scan.create(title=title, asset=asset)

    def _update_scan_progress(self, progress: str):
        """Update scan status to in progress"""
        database = models.Database()
        session = database.session
        scan = session.query(models.Scan).get(self._scan_db.id)
        scan.progress = progress
        session.commit()

    def _create_network(self):
        """Creates a docker swarm network where all services and agents can communicates."""
        if any(network.name == self.network for network in self._docker_client.networks.list()):
            logger.warning('network already exists.')
        else:
            logger.info('creating private network %s', self.network)
            return self._docker_client.networks.create(
                name=self.network,
                driver='overlay',
                attachable=True,
                labels={'ostorlab.universe': self.name},
                check_duplicate=True
            )

    def _start_services(self):
        """Start all the local runtime services."""
        self._start_mq_service()

    def _start_mq_service(self):
        """Start a local rabbitmq service."""
        self._mq_service = mq.LocalRabbitMQ(name=self.name, network=self.network)
        self._mq_service.start()
        if 'mq' in self.follow:
            self._log_streamer.stream(self._mq_service.service)

    def _check_services_healthy(self):
        """Check if the rabbitMQ service is running and healthy."""
        if self._mq_service is None or self._mq_service.is_healthy is False:
            raise UnhealthyService('MQ service is unhealthy.')

    def _check_agents_healthy(self):
        """Checks if an agent is healthy."""
        return self._are_agents_ready()

    def _start_agents(self, agent_group_definition: definitions.AgentGroupDefinition):
        """Starts all the agents as list in the agent run definition."""
        for agent in agent_group_definition.agents:
            self._start_agent(agent, extra_configs=[])

    def _start_pre_agents(self):
        """Starting pre-agents that must exist before other agents. This applies to all persistence
        agents that can start sending data at the start of the agent."""
        self._start_persist_vulnz_agent()

    def _start_post_agents(self):
        """Starting post-agents that must exist after other agents. This applies to the tracker
        that needs to monitor other agents."""
        self._start_tracker_agent()

    def _start_agent(self, agent: definitions.AgentSettings,
                     extra_configs: Optional[List[docker.types.ConfigReference]] = None) -> None:
        """Start agent based on provided definition.

        Args:
            agent: An agent definition containing all the settings of how agent should run and what arguments to pass.
        """
        logger.info('starting agent %s with %s', agent.key, agent.args)

        if _has_container_image(agent) is False:
            raise AgentNotInstalled(agent.key)

        runtime_agent = agent_runtime.AgentRuntime(agent, self.name, self._docker_client, self._mq_service)
        agent_service = runtime_agent.create_agent_service(self.network, extra_configs)
        if agent.key in self.follow:
            self._log_streamer.stream(agent_service)

        if agent.replicas > 1:
            # Ensure the agent service had to
            # TODO(alaeddine): Check if sleep if really needed and if it is, implement a parallel way to start agents
            #  and scale them.
            # time.sleep(10)
            self._scale_service(agent_service, agent.replicas)

    @tenacity.retry(stop=tenacity.stop_after_attempt(20),
                    wait=tenacity.wait_exponential(multiplier=1, max=12),
                    # return last value and don't raise RetryError exception.
                    retry_error_callback=lambda lv: lv.outcome.result(),
                    retry=tenacity.retry_if_result(lambda v: v is False))
    def _is_service_healthy(self, service: docker_models_services.Service, replicas=None) -> bool:
        """Checks if a docker service is healthy by checking all tasks status."""
        logger.debug('checking Spec service %s', service.name)
        try:
            if not replicas:
                replicas = service.attrs['Spec']['Mode']['Replicated']['Replicas']
            return replicas == len([task for task in service.tasks() if task['Status']['State'] == 'running'])
        except docker.errors.NotFound:
            return False

    def _list_agent_services(self):
        """List the services of type agents. All agent service must start with agent_."""
        services = self._docker_client.services.list(filters={'label': f'ostorlab.universe={self.name}'})
        for service in services:
            if service.name.startswith('agent_'):
                yield service

    def _start_tracker_agent(self):
        """Start the tracker agent to handle the scan lifecycle."""
        tracker_agent_settings = definitions.AgentSettings(key=TRACKER_AGENT_DEFAULT)
        self._start_agent(agent=tracker_agent_settings, extra_configs=[])

    def _start_persist_vulnz_agent(self):
        """Start the local persistence agent to dump vulnerabilities in the local config."""
        persist_vulnz_agent_settings = definitions.AgentSettings(
            key=LOCAL_PERSIST_VULNZ_AGENT_DEFAULT,
            mounts=[])
        self._start_agent(agent=persist_vulnz_agent_settings, extra_configs=[])

    def _inject_asset(self, asset: base_asset.Asset):
        """Injects the scan target assets."""
        asset_config = self._docker_client.configs.create(name=f'asset_{self.name}',
                                                          labels={'ostorlab.universe': self.name},
                                                          data=asset.to_proto())
        asset_config_reference = docker.types.ConfigReference(config_id=asset_config.id,
                                                              config_name=f'asset_{self.name}',
                                                              filename='/tmp/asset.binproto')
        selector_config = self._docker_client.configs.create(name=f'asset_selector_{self.name}',
                                                             labels={'ostorlab.universe': self.name},
                                                             data=asset.selector)
        selector_config_reference = docker.types.ConfigReference(config_id=selector_config.id,
                                                                 config_name=f'asset_selector_{self.name}',
                                                                 filename='/tmp/asset_selector.txt')
        inject_asset_agent_settings = definitions.AgentSettings(key=ASSET_INJECTION_AGENT_DEFAULT,
                                                                restart_policy='none')
        self._start_agent(agent=inject_asset_agent_settings,
                          extra_configs=[asset_config_reference, selector_config_reference])

    def _scale_service(self, service: docker_models_services.Service, replicas: int) -> None:
        """Calling scale directly on the service causes an API error. This is a workaround that simulates refreshing
         the service object, then calling the scale API."""
        for s in self._docker_client.services.list():
            if s.name == service.name:
                s.scale(replicas)

    def list(self, page: int = 1, number_elements: int = 10) -> List[runtime.Scan]:
        """Lists scans managed by runtime.

        Args:
            page: Page number for list pagination (default 1).
            number_elements: count of elements to show in the listed page (default 10).

        Returns:
            List of scan objects.
        """
        if page is not None:
            console.warning('Local runtime ignores scan list pagination')

        scans = {}
        database = models.Database()
        database.create_db_tables()
        session = database.session
        for s in session.query(models.Scan):
            scans[s.id] = runtime.Scan(
                id=s.id,
                asset=s.asset,
                created_time=s.created_time,
                progress=s.progress.value,
            )

        universe_ids = set()
        client = docker.from_env()
        services = client.services.list()

        for s in services:
            try:
                service_labels = s.attrs['Spec']['Labels']
                ostorlab_universe_id = service_labels.get('ostorlab.universe')
                if 'ostorlab.universe' in service_labels.keys() and ostorlab_universe_id not in universe_ids:
                    universe_ids.add(ostorlab_universe_id)
                    if ostorlab_universe_id.isnumeric() and int(ostorlab_universe_id) not in scans:
                        console.warning(f'Scan {ostorlab_universe_id} has not traced in DB.')
            except KeyError:
                logger.warning('The label ostorlab.universe do not exist.')

        return list(scans.values())

    @tenacity.retry(stop=tenacity.stop_after_attempt(20),
                    wait=tenacity.wait_exponential(multiplier=1, max=20),
                    # return last value and don't raise RetryError exception.
                    retry_error_callback=lambda lv: lv.outcome.result(),
                    retry=tenacity.retry_if_result(lambda v: v is False))
    def _are_agents_ready(self, fail_fast=True) -> bool:
        """Checks that all agents are ready and healthy while taking into account the run type of agent
         (once vs long-running)."""
        logger.info('listing services ...')
        agent_services = list(self._list_agent_services())
        for service in agent_services:
            logger.info('checking %s ...', service.name)
            if not _is_service_type_run(service):
                if self._is_service_healthy(service):
                    logger.info('agent service %s is healthy', service.name)
                else:
                    logger.error('agent service %s is not healthy', service.name)
                    if fail_fast:
                        return False
        return True

    def install(self) -> None:
        """Installs the default agents.

        Returns:
            None
        """
        for agent_key in DEFAULT_AGENTS:
            install_agent.install(agent_key=agent_key)
