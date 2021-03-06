"""
PyChromecast: remote control your Chromecast
"""
import logging
import fnmatch
from threading import Event

# pylint: disable=wildcard-import
import threading

import zeroconf

from .config import *  # noqa
from .error import *  # noqa
from . import socket_client
from .discovery import (  # noqa
    DISCOVER_TIMEOUT,
    CastListener,
    discover_chromecasts,
    start_discovery,
    stop_discovery,
)
from .dial import get_device_status, DeviceStatus
from .const import CAST_MANUFACTURERS, CAST_TYPES, CAST_TYPE_CHROMECAST
from .controllers.media import STREAM_TYPE_BUFFERED  # noqa

__all__ = ("__version__", "__version_info__", "get_chromecasts", "Chromecast")
__version_info__ = ("0", "7", "6")
__version__ = ".".join(__version_info__)

IDLE_APP_ID = "E8C28D3C"
IGNORE_CEC = []

_LOGGER = logging.getLogger(__name__)


def get_chromecast_from_host(host, tries=None, retry_wait=None, timeout=None):
    """Creates a Chromecast object from a zeroconf host."""
    # Build device status from the mDNS info, this information is
    # the primary source and the remaining will be fetched
    # later on.
    ip_address, port, uuid, model_name, friendly_name = host
    _LOGGER.debug("_get_chromecast_from_host %s", host)
    cast_type = CAST_TYPES.get(model_name.lower(), CAST_TYPE_CHROMECAST)
    manufacturer = CAST_MANUFACTURERS.get(model_name.lower(), "Google Inc.")
    device = DeviceStatus(
        friendly_name=friendly_name,
        model_name=model_name,
        manufacturer=manufacturer,
        uuid=uuid,
        cast_type=cast_type,
    )
    return Chromecast(
        host=ip_address,
        port=port,
        device=device,
        tries=tries,
        timeout=timeout,
        retry_wait=retry_wait,
    )


# Alias for backwards compatibility
_get_chromecast_from_host = get_chromecast_from_host  # pylint: disable=invalid-name


def get_chromecast_from_service(
    services, zconf, tries=None, retry_wait=None, timeout=None
):
    """Creates a Chromecast object from a zeroconf service."""
    # Build device status from the mDNS service name info, this
    # information is the primary source and the remaining will be
    # fetched later on.
    services, uuid, model_name, friendly_name, _, _ = services
    _LOGGER.debug("_get_chromecast_from_service %s", services)
    cast_type = CAST_TYPES.get(model_name.lower(), CAST_TYPE_CHROMECAST)
    manufacturer = CAST_MANUFACTURERS.get(model_name.lower(), "Google Inc.")
    device = DeviceStatus(
        friendly_name=friendly_name,
        model_name=model_name,
        manufacturer=manufacturer,
        uuid=uuid,
        cast_type=cast_type,
    )
    return Chromecast(
        host=None,
        device=device,
        tries=tries,
        timeout=timeout,
        retry_wait=retry_wait,
        services=services,
        zconf=zconf,
    )


# Alias for backwards compatibility
_get_chromecast_from_service = (  # pylint: disable=invalid-name
    get_chromecast_from_service
)


def get_listed_chromecasts(
    friendly_names=None,
    uuids=None,
    tries=None,
    retry_wait=None,
    timeout=None,
    discovery_timeout=DISCOVER_TIMEOUT,
):
    """
    Searches the network for chromecast devices matching a list of friendly
    names or a list of UUIDs.

    Returns a tuple of:
      A list of Chromecast objects matching the criteria,
      or an empty list if no matching chromecasts were found.
      A service browser to keep the Chromecast mDNS data updated. When updates
      are (no longer) needed, pass the broswer object to
      pychromecast.discovery.stop_discover().

    To only discover chromcast devices wihtout connecting to them, use
    discover_listed_chromecasts instead.

    :param friendly_names: A list of wanted friendly names
    :param uuids: A list of wanted uuids
    :param tries: passed to get_chromecasts
    :param retry_wait: passed to get_chromecasts
    :param timeout: passed to get_chromecasts
    :param discovery_timeout: A floating point number specifying the time to wait
                               devices matching the criteria have been found.
    """

    cc_list = {}

    def callback(uuid, name):  # pylint: disable=unused-argument
        _LOGGER.debug("Found chromecast %s", uuid)

        def get_chromecast_from_uuid(uuid):
            return get_chromecast_from_service(
                listener.services[uuid],
                zconf=zconf,
                tries=tries,
                retry_wait=retry_wait,
                timeout=timeout,
            )

        service = listener.services[uuid]
        friendly_name = service[3]
        try:
            if uuids and uuid in uuids:
                if uuid not in cc_list:
                    cc_list[uuid] = get_chromecast_from_uuid(uuid)
                uuids.remove(uuid)
            if friendly_names and friendly_name in friendly_names:
                if uuid not in cc_list:
                    cc_list[uuid] = get_chromecast_from_uuid(uuid)
                friendly_names.remove(friendly_name)
            if not friendly_names and not uuids:
                discover_complete.set()
        except ChromecastConnectionError:  # noqa
            pass

    discover_complete = Event()

    listener = CastListener(callback)
    zconf = zeroconf.Zeroconf()
    browser = start_discovery(listener, zconf)

    # Wait for the timeout or found all wanted devices
    discover_complete.wait(discovery_timeout)
    return (cc_list.values(), browser)


# pylint: disable=too-many-locals
def get_chromecasts(
    tries=None, retry_wait=None, timeout=None, blocking=True, callback=None
):
    """
    Searches the network for chromecast devices and creates a Chromecast object
    for each discovered device.

    Returns a tuple of:
      A list of Chromecast objects, or an empty list if no matching chromecasts were
      found.
      A service browser to keep the Chromecast mDNS data updated. When updates
      are (no longer) needed, pass the broswer object to
      pychromecast.discovery.stop_discover().

    To only discover chromcast devices wihtout connecting to them, use
    discover_chromecasts instead.

    Parameters tries, timeout, retry_wait and blocking_app_launch controls the
    behavior of the created Chromecast instances.

    :param tries: Number of retries to perform if the connection fails.
                  None for inifinite retries.
    :param timeout: A floating point number specifying the socket timeout in
                    seconds. None means to use the default which is 30 seconds.
    :param retry_wait: A floating point number specifying how many seconds to
                       wait between each retry. None means to use the default
                       which is 5 seconds.
    :param blocking: If True, returns a list of discovered chromecast devices.
                     If False, triggers a callback for each discovered chromecast,
                     and returns a function which can be executed to stop discovery.
    :param callback: Callback which is triggerd for each discovered chromecast when
                     blocking = False.
    """
    if blocking:
        # Thread blocking chromecast discovery
        services, browser = discover_chromecasts()
        cc_list = []
        for service in services:
            try:
                cc_list.append(
                    get_chromecast_from_service(
                        service,
                        browser.zc,
                        tries=tries,
                        retry_wait=retry_wait,
                        timeout=timeout,
                    )
                )
            except ChromecastConnectionError:  # noqa
                pass
        return (cc_list, browser)

    # Callback based chromecast discovery
    if not callable(callback):
        raise ValueError("Nonblocking discovery requires a callback function.")

    def internal_callback(uuid, name):  # pylint: disable=unused-argument
        """Called when zeroconf has discovered a new chromecast."""
        try:
            callback(
                get_chromecast_from_service(
                    listener.services[uuid],
                    zconf=zconf,
                    tries=tries,
                    retry_wait=retry_wait,
                    timeout=timeout,
                )
            )
        except ChromecastConnectionError:  # noqa
            pass

    listener = CastListener(internal_callback)
    zconf = zeroconf.Zeroconf()
    browser = start_discovery(listener, zconf)
    return browser


# pylint: disable=too-many-instance-attributes, too-many-public-methods
class Chromecast:
    """
    Class to interface with a ChromeCast.

    :param host: The host to connect to.
    :param port: The port to use when connecting to the device, set to None to
                 use the default of 8009. Special devices such as Cast Groups
                 may return a different port number so we need to use that.
    :param device: DeviceStatus with initial information for the device.
    :type device: pychromecast.dial.DeviceStatus
    :param tries: Number of retries to perform if the connection fails.
                  None for inifinite retries.
    :param timeout: A floating point number specifying the socket timeout in
                    seconds. None means to use the default which is 30 seconds.
    :param retry_wait: A floating point number specifying how many seconds to
                       wait between each retry. None means to use the default
                       which is 5 seconds.
    :param services: A list of mDNS services to try to connect to. If present,
                     parameters host and port are ignored and host and port are
                     instead resolved through mDNS. The list of services may be
                     modified, for example if speaker group leadership is handed
                     over. SocketClient will catch modifications to the list when
                     attempting reconnect.
    :param zconf: A zeroconf instance, needed if a list of services is passed.
                  The zeroconf instance may be obtained from the browser returned by
                  pychromecast.start_discovery().
    """

    def __init__(self, host, port=None, device=None, **kwargs):
        tries = kwargs.pop("tries", None)
        timeout = kwargs.pop("timeout", None)
        retry_wait = kwargs.pop("retry_wait", None)
        services = kwargs.pop("services", None)
        zconf = kwargs.pop("zconf", None)

        self.logger = logging.getLogger(__name__)

        # Resolve host to IP address
        self._services = services
        self.host = host
        self.port = port or 8009

        self.logger.info("Querying device status")
        self.device = device
        if device:
            dev_status = get_device_status(self.host, services, zconf)
            if dev_status:
                # Values from `device` have priority over `dev_status`
                # as they come from the dial information.
                # `dev_status` may add extra information such as `manufacturer`
                # which dial does not supply
                self.device = DeviceStatus(
                    friendly_name=(device.friendly_name or dev_status.friendly_name),
                    model_name=(device.model_name or dev_status.model_name),
                    manufacturer=(device.manufacturer or dev_status.manufacturer),
                    uuid=(device.uuid or dev_status.uuid),
                    cast_type=(device.cast_type or dev_status.cast_type),
                )
            else:
                self.device = device
        else:
            self.device = get_device_status(self.host, services, zconf)

        if not self.device:
            raise ChromecastConnectionError(  # noqa
                "Could not connect to {}:{}".format(self.host, self.port)
            )

        self.status = None
        self.status_event = threading.Event()

        self.socket_client = socket_client.SocketClient(
            host,
            port=port,
            cast_type=self.device.cast_type,
            tries=tries,
            timeout=timeout,
            retry_wait=retry_wait,
            services=services,
            zconf=zconf,
        )

        receiver_controller = self.socket_client.receiver_controller
        receiver_controller.register_status_listener(self)

        # Forward these methods
        self.set_volume = receiver_controller.set_volume
        self.set_volume_muted = receiver_controller.set_volume_muted
        self.play_media = self.socket_client.media_controller.play_media
        self.register_handler = self.socket_client.register_handler
        self.register_status_listener = receiver_controller.register_status_listener
        self.register_launch_error_listener = (
            receiver_controller.register_launch_error_listener
        )
        self.register_connection_listener = (
            self.socket_client.register_connection_listener
        )

    @property
    def ignore_cec(self):
        """ Returns whether the CEC data should be ignored. """
        return self.device is not None and any(
            [
                fnmatch.fnmatchcase(self.device.friendly_name, pattern)
                for pattern in IGNORE_CEC
            ]
        )

    @property
    def is_idle(self):
        """ Returns if there is currently an app running. """
        return (
            self.status is None
            or self.app_id in (None, IDLE_APP_ID)
            or (
                self.cast_type == CAST_TYPE_CHROMECAST
                and not self.status.is_active_input
                and not self.ignore_cec
            )
        )

    @property
    def uuid(self):
        """ Returns the unique UUID of the Chromecast device. """
        return self.device.uuid

    @property
    def name(self):
        """
        Returns the friendly name set for the Chromecast device.
        This is the name that the end-user chooses for the cast device.
        """
        return self.device.friendly_name

    @property
    def uri(self):
        """ Returns the device URI (ip:port) """
        return "{}:{}".format(self.host, self.port)

    @property
    def model_name(self):
        """ Returns the model name of the Chromecast device. """
        return self.device.model_name

    @property
    def cast_type(self):
        """
        Returns the type of the Chromecast device.
        This is one of CAST_TYPE_CHROMECAST for regular Chromecast device,
        CAST_TYPE_AUDIO for Chromecast devices that only support audio
        and CAST_TYPE_GROUP for virtual a Chromecast device that groups
        together two or more cast (Audio for now) devices.

        :rtype: str
        """
        return self.device.cast_type

    @property
    def app_id(self):
        """ Returns the current app_id. """
        return self.status.app_id if self.status else None

    @property
    def app_display_name(self):
        """ Returns the name of the current running app. """
        return self.status.display_name if self.status else None

    @property
    def media_controller(self):
        """ Returns the media controller. """
        return self.socket_client.media_controller

    def new_cast_status(self, status):
        """ Called when a new status received from the Chromecast. """
        self.status = status
        if status:
            self.status_event.set()

    def start_app(self, app_id, force_launch=False):
        """ Start an app on the Chromecast. """
        self.logger.info("Starting app %s", app_id)

        self.socket_client.receiver_controller.launch_app(app_id, force_launch)

    def quit_app(self):
        """ Tells the Chromecast to quit current app_id. """
        self.logger.info("Quiting current app")

        self.socket_client.receiver_controller.stop_app()

    def volume_up(self, delta=0.1):
        """ Increment volume by 0.1 (or delta) unless it is already maxed.
        Returns the new volume.

        """
        if delta <= 0:
            raise ValueError(
                "volume delta must be greater than zero, not {}".format(delta)
            )
        return self.set_volume(self.status.volume_level + delta)

    def volume_down(self, delta=0.1):
        """ Decrement the volume by 0.1 (or delta) unless it is already 0.
        Returns the new volume.
        """
        if delta <= 0:
            raise ValueError(
                "volume delta must be greater than zero, not {}".format(delta)
            )
        return self.set_volume(self.status.volume_level - delta)

    def wait(self, timeout=None):
        """
        Waits until the cast device is ready for communication. The device
        is ready as soon a status message has been received.

        If the worker thread is not already running, it will be started.

        If the status has already been received then the method returns
        immediately.

        :param timeout: a floating point number specifying a timeout for the
                        operation in seconds (or fractions thereof). Or None
                        to block forever.
        """
        if not self.socket_client.isAlive():
            self.socket_client.start()
        self.status_event.wait(timeout=timeout)

    def connect(self):
        """ Connect to the chromecast.

            Must only be called if the worker thread will not be started.
        """
        self.socket_client.connect()

    def disconnect(self, timeout=None, blocking=True):
        """
        Disconnects the chromecast and waits for it to terminate.

        :param timeout: a floating point number specifying a timeout for the
                        operation in seconds (or fractions thereof). Or None
                        to block forever.
        :param blocking: If True it will block until the disconnection is
                         complete, otherwise it will return immediately.
        """
        self.socket_client.disconnect(blocking)
        if blocking:
            self.join(timeout=timeout)

    def join(self, timeout=None):
        """
        Blocks the thread of the caller until the chromecast connection is
        stopped.

        :param timeout: a floating point number specifying a timeout for the
                        operation in seconds (or fractions thereof). Or None
                        to block forever.
        """
        self.socket_client.join(timeout=timeout)

    def start(self):
        """
        Start the chromecast connection's worker thread.
        """
        self.socket_client.start()

    def __del__(self):
        try:
            self.socket_client.stop.set()
        except AttributeError:
            pass

    def __repr__(self):
        txt = "Chromecast({!r}, port={!r}, device={!r})".format(
            self.host, self.port, self.device
        )
        return txt

    def __unicode__(self):
        return "Chromecast({}, {}, {}, {}, {})".format(
            self.host,
            self.port,
            self.device.friendly_name,
            self.device.model_name,
            self.device.manufacturer,
        )
