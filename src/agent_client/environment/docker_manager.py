import os
import shutil
import time
from typing import Dict
from weakref import finalize

import docker
from docker.errors import APIError, ImageNotFound
from docker.types import Mount
from docker.models.containers import Container

import logging


class DockerManager:
    """
    Helper class for orchestrating the Docker containers to execute the agent's actions in.
    """

    def __init__(self, image: str, env_vars: Dict[str, str], container_start_timeout: int):
        self.image = image
        self.env_vars = env_vars
        self.container_start_timeout = container_start_timeout
        self.container = None
        self.agent_repo_dir = 'agent_work_dir'

        self.client = docker.from_env()

        # Note that this will not work while debugging if the process is manually interrupted
        finalize(self, self._cleanup_container)

    def _cleanup_container(self):
        self._stop_and_remove_container()
        self._clear_directory()

    def _clear_directory(self):
        for filename in os.listdir(self.agent_repo_dir):
            file_path = os.path.join(self.agent_repo_dir, filename)
            try:
                if os.path.isdir(file_path):
                    shutil.rmtree(file_path)  # Remove directories
            except Exception as e:
                print(f'Failed to delete {file_path}. Reason: {e}')

    def _stop_and_remove_container(self):
        """
        Stops and removes a running container.

        If the container is in "running" state, it will be stopped and then removed.
        Otherwise, it simply removes the container.
        """
        if self.container.status == "running":
            self.container.stop()
        self.container.remove()

    def setup_image(self):
        """
        Sets up the image to launch a container from.

        This method first checks if the specified image is available locally. If the image is not found locally
        it will attempt to pull the image from Docker Hub. The image name may include a tag, which is extracted if present.
        If the pull operation encounters an API error, the error is logged, and the error is re-raised.

        Raises:
            APIError: If there is an error during the image pull operation from Docker Hub.
        """
        try:
            self.client.images.get(self.image)
            logging.info(f'Found {self.image} locally and using that version of it.')
        except (ImageNotFound, APIError):
            logging.info(f'Found {self.image} not found locally. Attempting to pull from docker hub.')
            repository, tag = self.image, None
            if ":" in repository:
                repository, tag = repository.split(":")
            try:
                self.client.images.pull(repository=repository, tag=tag)
            except APIError as e:
                logging.error(e)
                logging.error("Please verify that you passed a valid Docker Hub image name and tag. Make sure"
                              "the image is available on the Docker Hub.")
                raise e

    def create_container(self) -> Container:
        """
        Creates a container, sets self.container to it and returns it.

        Creates a container with the specified image and environment variables. Runs "tail -f /dev/null" via
        the entrypoint parameter when the container is started. This function does not start the container!

        The specified script is a minimalistic way to keep the container alive, which allows us
        to continuously execute terminal commands provided by the agent.

        Returns:
            (Container) The created container object.

        Raises:
            APIError: If a Docker API error occurred while creating the container.
        """
        try:
            self.container = self.client.containers.create(
                image=self.image, environment=self.env_vars, detach=True, entrypoint="tail -f /dev/null"
            )
            return self.container
        except APIError as e:
            logging.error(f'Docker error occurred while creating the container: {e}')
            raise e

    def start_container(self):
        """
        Starts the Docker container and waits until it is running or the start timeout is reached.

        If the container status is "created", it will start the container. It then enters a loop
        that repeatedly checks the container's status until it is either running, has exited, or
        the timeout period is exceeded.

        Raises:
            RuntimeError: If the container is not yet created, fails to start or exits immediately after starting.
        """
        if self.container.status == "created":
            # Now the command specified in entrypoint in create_container() is executed
            self.container.start()
        else:
            logging.error('Attempted to start Docker container before creating it.')
            raise RuntimeError("Attempted to start Docker container before creating it.")

        start_time = time.time()
        while time.time() - start_time < self.container_start_timeout:
            self.container.reload()
            if self.container.status == "running":
                logging.info(f"Container started successfully")
                return self.container
            elif self.container.status == "exited":
                logging.error(f"Container exited on start.")
                logging.error(f"Container logs: {self.container.logs()}")
                raise RuntimeError("Could not start self.container.")
            time.sleep(0.1)

        logging.error(f"Container failed to start within the timeout period")
        raise RuntimeError("Could not start container.")

    def run_container(self):
        """
        Attempts to launch a container with the specified image and environment variables. This is similar to
        creating and starting the container, but the Python SDK offers a richer feature set for the run function.

        Additionally sets up a bind mount between '/usr/code' in the container and the vcs-agent project's
        'repo' folder. This is a workaround for mapping agent's actions into the container environment. Using
        shell scripting didnt work. A cleaner approach would be to just run the entire client in the container.

        Raises:
            RuntimeError: If the container is not yet created, fails to start or exits immediately after starting.
        """
        try:
            self.container = self.client.containers.run(self.image, environment=self.env_vars, detach=True,
                                                    entrypoint="tail -f /dev/null",
                                                    mounts=[Mount(target='/usr/code',
                                                    source='~/vcs-agent/agent_work_dir',
                                                    type='bind')])
        except APIError as e:
            logging.error(f'Docker error occurred while running (create+start) the container: {e}')
            raise e

        start_time = time.time()
        while time.time() - start_time < self.container_start_timeout:
            self.container.reload()
            if self.container.status == "running":
                logging.info(f"Container started successfully")
                return self.container
            elif self.container.status == "exited":
                logging.error(f"Container exited on start.")
                logging.error(f"Container logs: {self.container.logs()}")
                raise RuntimeError("Could not start self.container.")
            time.sleep(0.1)

        logging.error(f"Container failed to start within the timeout period")
        raise RuntimeError("Could not start container.")
