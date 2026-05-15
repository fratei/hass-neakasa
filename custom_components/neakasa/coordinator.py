from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Optional, Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_FRIENDLY_NAME,
    CONF_USERNAME,
    CONF_PASSWORD,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from datetime import datetime

from .api import NeakasaAPI, APIAuthError, APIConnectionError
from .value_cacher import ValueCacher
from .const import DOMAIN, _LOGGER

@dataclass
class NeakasaAPIData:
    """Class to hold api data.

    All fields are Optional to support device variants (e.g. M1 Lite) that do
    not expose every property advertised by the full M1 model. Missing values
    default to None / 0; downstream entity classes already handle None via
    `getattr(..., None)` and entity_registry_enabled_default.
    """

    binFullWaitReset: Optional[bool] = None
    cleanCfg: Optional[dict[str, Any]] = None
    sandLevelState: Optional[int] = None
    sandLevelPercent: Optional[int] = None
    bucketStatus: Optional[int] = None
    room_of_bin: Optional[int] = None
    youngCatMode: Optional[bool] = None
    childLockOnOff: Optional[bool] = None
    autoBury: Optional[bool] = None
    autoLevel: Optional[bool] = None
    silentMode: Optional[bool] = None
    wifiRssi: Optional[int] = None
    autoForceInit: Optional[bool] = None
    bIntrptRangeDet: Optional[bool] = None
    stayTime: int = 0
    lastUse: Optional[int] = None
    cat_list: list[object] = field(default_factory=list)
    record_list: list[object] = field(default_factory=list)


def _get_value(devicedata: dict, key: str, default: Any = None) -> Any:
    """Safely fetch devicedata[key]['value']. Returns default if missing.

    The Neakasa cloud omits properties that the device does not support
    (e.g. M1 Lite does not expose cleanCfg). The original code accessed
    these keys directly and crashed with KeyError on Lite hardware.
    """
    entry = devicedata.get(key)
    if not isinstance(entry, dict):
        return default
    return entry.get('value', default)


def _get_bool(devicedata: dict, key: str) -> Optional[bool]:
    """Return True/False/None for boolean-like fields (value == 1)."""
    value = _get_value(devicedata, key)
    if value is None:
        return None
    return value == 1


def _get_nested(devicedata: dict, key: str, *subkeys, default: Any = None) -> Any:
    """Safely fetch devicedata[key]['value'][subkey...]."""
    value = _get_value(devicedata, key)
    for sub in subkeys:
        if not isinstance(value, dict):
            return default
        value = value.get(sub)
    return value if value is not None else default


def _build_api_data(devicedata: dict, records: dict, last_use: Optional[int]) -> NeakasaAPIData:
    """Build NeakasaAPIData from device properties, tolerating missing fields.

    Used in all three code paths (initial fetch, post-auth-retry, post-identity-retry)
    to keep behaviour consistent and avoid duplicating defensive access logic.
    """
    return NeakasaAPIData(
        binFullWaitReset=_get_bool(devicedata, 'binFullWaitReset'),
        cleanCfg=_get_value(devicedata, 'cleanCfg'),
        youngCatMode=_get_bool(devicedata, 'youngCatMode'),
        childLockOnOff=_get_bool(devicedata, 'childLockOnOff'),
        autoBury=_get_bool(devicedata, 'autoBury'),
        autoLevel=_get_bool(devicedata, 'autoLevel'),
        silentMode=_get_bool(devicedata, 'silentMode'),
        autoForceInit=_get_bool(devicedata, 'autoForceInit'),
        bIntrptRangeDet=_get_bool(devicedata, 'bIntrptRangeDet'),
        sandLevelPercent=_get_nested(devicedata, 'Sand', 'percent'),
        wifiRssi=_get_nested(devicedata, 'NetWorkStatus', 'WiFi_RSSI'),
        bucketStatus=_get_value(devicedata, 'bucketStatus'),
        room_of_bin=_get_value(devicedata, 'room_of_bin'),
        sandLevelState=_get_nested(devicedata, 'Sand', 'level'),
        stayTime=_get_nested(devicedata, 'catLeft', 'stayTime', default=0) or 0,
        lastUse=last_use,
        cat_list=records.get('cat_list', []) if isinstance(records, dict) else [],
        record_list=records.get('record_list', []) if isinstance(records, dict) else [],
    )


class NeakasaCoordinator(DataUpdateCoordinator):
    """My coordinator."""

    data: NeakasaAPIData

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""

        # Set variables from values entered in config flow setup
        self.deviceid = config_entry.data[CONF_DEVICE_ID]
        self.devicename = config_entry.data[CONF_FRIENDLY_NAME]
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]

        self._deviceName = None
        self.lastUseDate = None

        self._recordsCache = ValueCacher(refresh_after=timedelta(minutes=30), discard_after=timedelta(hours=4))
        self._devicePropertiesCache = ValueCacher(refresh_after=timedelta(seconds=0), discard_after=timedelta(minutes=30))

        # Initialise DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            # Method to call on every update interval.
            update_method=self.async_update_data,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=60),
        )

        # API will be obtained from the shared manager when needed
        self.api = None
        

    async def setProperty(self, key: str, value: Any):
        from . import get_shared_api
        api = await get_shared_api(self.hass, self.username, self.password)
        await api.setDeviceProperties(self.deviceid, {key: value})
        #update data
        setattr(self.data, key, value)
        self.async_set_updated_data(self.data)

    async def invokeService(self, service: str):
        from . import get_shared_api
        api = await get_shared_api(self.hass, self.username, self.password)
        match service:
            case 'clean':
                return await api.cleanNow(self.deviceid)
            case 'level':
                return await api.sandLeveling(self.deviceid)
        raise Exception('cannot find service to invoke')

    async def _getDeviceName(self):
        if self._deviceName is not None:
            return self._deviceName

        """get deviceName by iotId"""
        from . import get_shared_api
        api = await get_shared_api(self.hass, self.username, self.password)
        devices = await api.getDevices()
        devices = list(filter(lambda devices: devices['iotId'] == self.deviceid, devices))
        if(len(devices) == 0):
            raise APIConnectionError("iotId not found in device list")
        deviceName = devices[0]['deviceName']
        self._deviceName = deviceName
        return deviceName

    async def _getRecords(self):
        async def fetch():
            deviceName = await self._getDeviceName()
            from . import get_shared_api
            api = await get_shared_api(self.hass, self.username, self.password)
            return await api.getRecords(deviceName)

        return await self._recordsCache.get_or_update(fetch)

    async def _getDeviceProperties(self):
        async def fetch():
            from . import get_shared_api
            api = await get_shared_api(self.hass, self.username, self.password)
            return await api.getDeviceProperties(self.deviceid)

        return await self._devicePropertiesCache.get_or_update(fetch)

    def _extract_last_use(self, devicedata: dict) -> Optional[int]:
        """Extract catLeft.time, tolerating missing key on Lite devices."""
        cat_left = devicedata.get('catLeft')
        if not isinstance(cat_left, dict):
            return None
        return cat_left.get('time')

    async def async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            devicedata = await self._getDeviceProperties()

            newLastUseDate = self._extract_last_use(devicedata)

            if self.lastUseDate != newLastUseDate:
                self._recordsCache.mark_as_stale()

            self.lastUseDate = newLastUseDate

            records = await self._getRecords()

            try:
                return _build_api_data(devicedata, records, newLastUseDate)
            except Exception as err:
                _LOGGER.error(err)
                # This will show entities as unavailable by raising UpdateFailed exception
                raise UpdateFailed(f"Got no data from api, please try to restart your litter box.") from err
        except APIAuthError as err:
            _LOGGER.warning(f"Authentication error for device {self.devicename}, attempting to reconnect: {err}")
            try:
                # Force reconnection of the API
                from . import force_reconnect_api
                api = await force_reconnect_api(self.hass, self.username, self.password)
                _LOGGER.info(f"Successfully reconnected API for device {self.devicename}")
                # Retry the data fetch after reconnection
                devicedata = await api.getDeviceProperties(self.deviceid)
                newLastUseDate = self._extract_last_use(devicedata)
                if self.lastUseDate != newLastUseDate:
                    self._recordsCache.mark_as_stale()
                self.lastUseDate = newLastUseDate
                records = await self._getRecords()

                return _build_api_data(devicedata, records, newLastUseDate)
            except Exception as reconnect_err:
                _LOGGER.error(f"Failed to reconnect API for device {self.devicename}: {reconnect_err}")
                raise UpdateFailed(f"Authentication failed and reconnection failed: {err}") from err
        except APIConnectionError as err:
            # Check if this is an identityId error, which indicates authentication issues
            if "identityId is blank" in str(err):
                _LOGGER.debug(f"IdentityId error for device {self.devicename}, attempting automatic reconnection")
                try:
                    # Clear the shared API to force a fresh connection
                    from . import clear_shared_api, force_reconnect_api
                    clear_shared_api(self.username, self.password)
                    api = await force_reconnect_api(self.hass, self.username, self.password)
                    _LOGGER.debug(f"Successfully reconnected API for device {self.devicename}")
                    # Retry the data fetch after reconnection
                    devicedata = await api.getDeviceProperties(self.deviceid)
                    newLastUseDate = self._extract_last_use(devicedata)
                    if self.lastUseDate != newLastUseDate:
                        self._recordsCache.mark_as_stale()
                    self.lastUseDate = newLastUseDate
                    records = await self._getRecords()

                    return _build_api_data(devicedata, records, newLastUseDate)
                except Exception as reconnect_err:
                    _LOGGER.error(f"Failed to reconnect API after identityId error for device {self.devicename}: {reconnect_err}")
                    raise UpdateFailed(f"IdentityId error and reconnection failed: {err}") from err
            else:
                _LOGGER.error(f"API connection error for device {self.devicename}: {err}")
                raise UpdateFailed(err) from err
