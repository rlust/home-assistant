"""Code to handle a Hue bridge."""
import asyncio

import aiohue
import async_timeout
import slugify as unicode_slug
import voluptuous as vol

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, config_validation as cv

from .const import DOMAIN, LOGGER
from .errors import AuthenticationRequired, CannotConnect
from .helpers import create_config_flow

SERVICE_HUE_SCENE = "hue_activate_scene"
ATTR_GROUP_NAME = "group_name"
ATTR_SCENE_NAME = "scene_name"
SCENE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_GROUP_NAME): cv.string, vol.Required(ATTR_SCENE_NAME): cv.string}
)


class HueBridge:
    """Manages a single Hue bridge."""

    def __init__(self, hass, config_entry, allow_unreachable, allow_groups):
        """Initialize the system."""
        self.config_entry = config_entry
        self.hass = hass
        self.allow_unreachable = allow_unreachable
        self.allow_groups = allow_groups
        self.available = True
        self.authorized = False
        self.api = None
        self.parallel_updates_semaphore = None

    @property
    def host(self):
        """Return the host of this bridge."""
        return self.config_entry.data["host"]

    async def async_setup(self, tries=0):
        """Set up a phue bridge based on host parameter."""
        host = self.host
        hass = self.hass

        try:
            self.api = await get_bridge(hass, host, self.config_entry.data["username"])
        except AuthenticationRequired:
            # Usernames can become invalid if hub is reset or user removed.
            # We are going to fail the config entry setup and initiate a new
            # linking procedure. When linking succeeds, it will remove the
            # old config entry.
            create_config_flow(hass, host)
            return False

        except CannotConnect:
            LOGGER.error("Error connecting to the Hue bridge at %s", host)
            raise ConfigEntryNotReady

        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("Unknown error connecting with Hue bridge at %s", host)
            return False

        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(self.config_entry, "light")
        )
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(
                self.config_entry, "binary_sensor"
            )
        )
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(self.config_entry, "sensor")
        )

        hass.services.async_register(
            DOMAIN, SERVICE_HUE_SCENE, self.hue_activate_scene, schema=SCENE_SCHEMA
        )

        self.parallel_updates_semaphore = asyncio.Semaphore(
            3 if self.api.config.modelid == "BSB001" else 10
        )

        self.authorized = True
        return True

    async def async_request_call(self, coro):
        """Process request batched."""

        async with self.parallel_updates_semaphore:
            return await coro

    async def async_reset(self):
        """Reset this bridge to default state.

        Will cancel any scheduled setup retry and will unload
        the config entry.
        """
        # The bridge can be in 3 states:
        #  - Setup was successful, self.api is not None
        #  - Authentication was wrong, self.api is None, not retrying setup.

        # If the authentication was wrong.
        if self.api is None:
            return True

        self.hass.services.async_remove(DOMAIN, SERVICE_HUE_SCENE)

        # If setup was successful, we set api variable, forwarded entry and
        # register service
        results = await asyncio.gather(
            self.hass.config_entries.async_forward_entry_unload(
                self.config_entry, "light"
            ),
            self.hass.config_entries.async_forward_entry_unload(
                self.config_entry, "binary_sensor"
            ),
            self.hass.config_entries.async_forward_entry_unload(
                self.config_entry, "sensor"
            ),
        )
        # None and True are OK
        return False not in results

    async def hue_activate_scene(self, call, updated=False):
        """Service to call directly into bridge to set scenes."""
        group_name = call.data[ATTR_GROUP_NAME]
        scene_name = call.data[ATTR_SCENE_NAME]

        group = next(
            (group for group in self.api.groups.values() if group.name == group_name),
            None,
        )

        # Additional scene logic to handle duplicate scene names across groups
        scene = next(
            (
                scene
                for scene in self.api.scenes.values()
                if scene.name == scene_name
                and group is not None
                and sorted(scene.lights) == sorted(group.lights)
            ),
            None,
        )

        # If we can't find it, fetch latest info.
        if not updated and (group is None or scene is None):
            await self.api.groups.update()
            await self.api.scenes.update()
            await self.hue_activate_scene(call, updated=True)
            return

        if group is None:
            LOGGER.warning("Unable to find group %s", group_name)
            return

        if scene is None:
            LOGGER.warning("Unable to find scene %s", scene_name)
            return

        await group.set_action(scene=scene.id)

    async def handle_unauthorized_error(self):
        """Create a new config flow when the authorization is no longer valid."""
        if not self.authorized:
            # we already created a new config flow, no need to do it again
            return
        LOGGER.error(
            "Unable to authorize to bridge %s, setup the linking again.", self.host
        )
        self.authorized = False
        create_config_flow(self.hass, self.host)


async def get_bridge(hass, host, username=None):
    """Create a bridge object and verify authentication."""
    bridge = aiohue.Bridge(
        host, username=username, websession=aiohttp_client.async_get_clientsession(hass)
    )

    try:
        with async_timeout.timeout(10):
            # Create username if we don't have one
            if not username:
                device_name = unicode_slug.slugify(
                    hass.config.location_name, max_length=19
                )
                await bridge.create_user(f"home-assistant#{device_name}")

            # Initialize bridge (and validate our username)
            await bridge.initialize()

        return bridge
    except (aiohue.LinkButtonNotPressed, aiohue.Unauthorized):
        raise AuthenticationRequired
    except (asyncio.TimeoutError, aiohue.RequestError):
        raise CannotConnect
    except aiohue.AiohueException:
        LOGGER.exception("Unknown Hue linking error occurred")
        raise AuthenticationRequired


def normalize_bridge_id(bridge_id: str):
    """Normalize a bridge identifier.

    There are three sources where we receive bridge ID from:
     - ssdp/upnp: <host>/description.xml, field root/device/serialNumber
     - nupnp: "id" field
     - Hue Bridge API: config.bridgeid

    The SSDP/UPNP source does not contain the middle 4 characters compared
    to the other sources. In all our tests the middle 4 characters are "fffe".
    """
    if len(bridge_id) == 16:
        return bridge_id[0:6] + bridge_id[-6:]

    if len(bridge_id) == 12:
        return bridge_id

    LOGGER.warning("Unexpected bridge id number found: %s", bridge_id)

    return bridge_id
