#SPDX-License-Identifier: MIT
"""
Augur library commands for controlling the backend components
"""

from copy import deepcopy
import os, time, atexit, subprocess, click, atexit, logging, sys
import psutil
import signal
import multiprocessing as mp
import gunicorn.app.base
from gunicorn.arbiter import Arbiter

from augur.cli import initialize_logging, pass_config, pass_application
from augur.housekeeper import Housekeeper
from augur.server import Server
from augur.application import Application
from augur.gunicorn import AugurGunicornApp

logger = logging.getLogger("augur")

@click.group('server', short_help='Server commands')
def cli():
    pass

@cli.command("start")
@click.option("--disable-housekeeper", is_flag=True, default=False, help="Turns off the housekeeper")
@click.option("--skip-cleanup", is_flag=True, default=False, help="Disables the old process cleanup that runs before Augur starts")
def start(disable_housekeeper, skip_cleanup):
    """
    Start Augur's backend server
    """
    augur_app = Application()

    if (sys.prefix == sys.base_prefix): #these ALWAYS differ when using a virtualenv
        logger.fatal("You are seeing this message because you installed Augur outside a virtual environment and are now attempting to run it.")
        logger.fatal("For safety reasons, Augur MUST be installed AND run in the same virtual environment. Please create one, activate it, re-install Augur, and then run Augur again.")
        logger.fatal("Please make sure to remove this installation after you create the virtual environment.")
        exit(None, None, None, 1)

    logger.info("Augur application initialized")
    logger.info(f"Using config file: {augur_app.config.config_file_location}")
    if not skip_cleanup:
        logger.debug("Stopping existing Augur instances...")
        _broadcast_signal_to_processes()
        time.sleep(2)
    else:
        logger.debug("Skipping process cleanup")

    master = initialize_components(augur_app, disable_housekeeper)

    logger.debug('Starting Gunicorn webserver...')
    logger.info(f"Augur's API server is running at: http://0.0.0.0:5000")
    logger.info("All API server logging (including errors) will be written to logs/gunicorn.log")
    logger.info('Housekeeper update process logs will now take over')
    Arbiter(master).run()

@cli.command('stop')
@initialize_logging
def stop_server():
    """
    Sends SIGTERM to all Augur server & worker processes
    """
    _broadcast_signal_to_processes(attach_logger=True)

@cli.command('kill')
@initialize_logging
def kill_server():
    """
    Sends SIGKILL to all Augur server & worker processes
    """
    _broadcast_signal_to_processes(signal=signal.SIGKILL, attach_logger=True)

@cli.command('processes',)
@initialize_logging
def list_processes():
    """
    Outputs the name and process ID (PID) of all currently running backend Augur processes, including any workers. Will only work in a virtual environment.
    """
    processes = get_augur_processes()
    for process in processes:
        logger.info(f"Found process {process.pid}")

def get_augur_processes():
    processes = []
    for process in psutil.process_iter(['cmdline', 'name', 'environ']):
        if process.info['cmdline'] is not None and process.info['environ'] is not None:
            try:
                if os.getenv('VIRTUAL_ENV') in process.info['environ']['VIRTUAL_ENV'] and 'python' in ''.join(process.info['cmdline'][:]).lower():
                    if process.pid != os.getpid():
                        processes.append(process)
            except KeyError:
                pass
    return processes

def _broadcast_signal_to_processes(signal=signal.SIGTERM, attach_logger=False):
    if attach_logger is True:
        _logger = logging.getLogger("augur")
    else:
        _logger = logger
    processes = get_augur_processes()
    if processes != []:
        for process in processes:
            if process.pid != os.getpid():
                logger.info(f"Stopping process {process.pid}")
                try:
                    process.send_signal(signal)
                except psutil.NoSuchProcess as e:
                    pass

def initialize_components(augur_app, disable_housekeeper):
    master = None
    manager = None
    broker = None
    housekeeper = None
    worker_processes = []
    mp.set_start_method('forkserver', force=True)

    if not disable_housekeeper:

        manager = mp.Manager()
        broker = manager.dict()
        housekeeper = Housekeeper(broker=broker, augur_app=augur_app)

        controller = augur_app.config.get_section('Workers')
        for worker in controller.keys():
            if controller[worker]['switch']:
                for i in range(controller[worker]['workers']):
                    logger.info("Booting {} #{}".format(worker, i + 1))
                    worker_process = mp.Process(target=worker_start, name=f"{worker}_{i}", kwargs={'worker_name': worker, 'instance_number': i, 'worker_port': controller[worker]['port']}, daemon=True)
                    worker_processes.append(worker_process)
                    worker_process.start()

    augur_app.manager = manager
    augur_app.broker = broker
    augur_app.housekeeper = housekeeper

    atexit._clear()
    atexit.register(exit, augur_app, worker_processes, master)
    return AugurGunicornApp(augur_app.gunicorn_options, augur_app=augur_app)

def worker_start(worker_name=None, instance_number=0, worker_port=None):
    try:
        time.sleep(30 * instance_number)
        destination = subprocess.DEVNULL
        process = subprocess.Popen("cd workers/{} && {}_start".format(worker_name,worker_name), shell=True, stdout=destination, stderr=subprocess.STDOUT)
        logger.info("{} #{} booted.".format(worker_name,instance_number+1))
    except KeyboardInterrupt as e:
        pass

def exit(augur_app, worker_processes, master, exit_code=0):

    if augur_app:
        logger.info("Shutdown started for this Gunicorn worker...")
        augur_app.shutdown()

    if worker_processes:
        for process in worker_processes:
            logger.debug("Shutting down worker process with pid: {}...".format(process.pid))
            process.terminate()

    if master is not None:
        logger.debug("Shutting down Gunicorn server")
        master.halt()

    logger.info("Shutdown complete")
    sys.exit(exit_code)
