"""Resolve complete customer addresses and cache coordinates for service-area checks."""

import re
import unicodedata
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

import structlog

from voice_agent.agent.services.composio import ComposioError, ComposioTools, find_key

EARTH_RADIUS_MILES = 3959
logger = structlog.get_logger()


class AddressError(ValueError):
    """An address remains pending; the message explains the reason to the agent."""


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    formatted: str = ""
    address: str = ""
    city: str = ""
    zip_code: str = ""

    def miles_to(self, other: "Location") -> float:
        lat1, lat2 = radians(self.latitude), radians(other.latitude)
        half_lat, half_lon = (lat2 - lat1) / 2, radians(other.longitude - self.longitude) / 2
        angle = sin(half_lat) ** 2 + cos(lat1) * cos(lat2) * sin(half_lon) ** 2
        return 2 * EARTH_RADIUS_MILES * asin(sqrt(min(1, angle)))


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c))))


def component(result: dict, kind: str, key: str = "long_name") -> str:
    return next(
        (item.get(key, "") for item in result.get("address_components", []) if kind in item.get("types", [])), ""
    )


def location(result: dict, *, city: str = "", zip_code: str = "") -> Location:
    point = result["geometry"]["location"]
    address = f"{component(result, 'street_number')} {component(result, 'route')}".strip()
    city = city or component(result, "locality") or component(result, "postal_town")
    zip_code = zip_code or component(result, "postal_code")
    formatted = ", ".join(part for part in (address, city, zip_code) if part)
    return Location(float(point["lat"]), float(point["lng"]), formatted, address, city, zip_code)


class Maps:
    def __init__(self, composio: ComposioTools):
        self.composio = composio
        self.known: dict[str, Location | None] = {}

    def _results(self, address: str) -> list[dict]:
        data = self.composio.execute("GOOGLE_MAPS_GEOCODING_API", {"address": address})
        status = find_key(data, "status")
        if status == "ZERO_RESULTS":
            return []
        if status not in (None, "OK"):
            raise ComposioError(f"Geocoding returned {status}")
        results = find_key(data, "results")
        if not isinstance(results, list):
            raise ComposioError("Geocoding returned no results list")
        return results

    def validate(self, address: str, city: str, zip_code: str) -> Location:
        """Accept one complete, non-partial US street match with the supplied city and ZIP.

        Inspect each result's own components, never mix fields from different candidates.
        https://developers.google.com/maps/documentation/geocoding/requests-geocoding
        """
        query = f"{address}, {city}, {zip_code}, USA"
        try:
            results = self._results(query)
            if not results:
                raise AddressError("no address was found for the supplied street, city, and ZIP code")
            if len(results) != 1:
                raise AddressError("the street, city, and ZIP code match more than one location")
            result = results[0]
            if result.get("partial_match"):
                raise AddressError("the street, city, and ZIP code matched only part of an address")
            number = component(result, "street_number")
            route = component(result, "route")
            if not number or not route:
                raise AddressError("the lookup found an area or road, but could not locate the street number")
            if component(result, "country", "short_name") != "US":
                raise AddressError("the address lookup did not find a US address")
            cities = {
                normalized(item.get(key, ""))
                for item in result.get("address_components", [])
                if set(item.get("types", [])) & {"locality", "postal_town", "sublocality", "sublocality_level_1"}
                for key in ("long_name", "short_name")
            }
            if normalized(city) not in cities:
                found_city = component(result, "locality") or component(result, "postal_town")
                raise AddressError(f"the city does not match the street address returned by the lookup ({found_city or 'no city returned'})")
            found_zip = component(result, "postal_code")
            if "-" in zip_code:
                found_zip += "-" + component(result, "postal_code_suffix")
            if zip_code != found_zip:
                raise AddressError(f"the ZIP code does not match the street address returned by the lookup ({found_zip or 'no ZIP returned'})")
            streets = {normalized(f"{number} {component(result, 'route', key)}") for key in ("long_name", "short_name")}
            if normalized(address) not in streets:
                raise AddressError(f"the lookup returned a different street address: {number} {route}; confirm the street")
            place = location(result, city=city, zip_code=zip_code)
        except (ComposioError, AttributeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, AddressError):
                raise
            logger.warning("geocoding_failed", error=str(error))
            raise AddressError("address checking is temporarily unavailable; the address has not been verified") from error
        self.known[f"{address}, {city}, {zip_code}".strip().lower()] = place
        self.known[place.formatted.lower()] = place
        return place

    def locate(self, address: str) -> Location | None:
        """Coordinates for coverage checks. Customer addresses have already been validated."""
        key = address.strip().lower()
        if not key:
            return None
        if key in self.known:
            return self.known[key]
        try:
            results = self._results(address)
            self.known[key] = location(results[0]) if results else None
        except (ComposioError, AttributeError, KeyError, TypeError, ValueError) as error:
            logger.warning("geocoding_failed", error=str(error))
            return None
        return self.known[key]
