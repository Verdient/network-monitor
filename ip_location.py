"""Resolve visible public peers from local GeoIP data, falling back to rate-limited HTTPS APIs."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from functools import lru_cache
from http.client import HTTPException
from ipaddress import ip_address, ip_network
import json
from pathlib import Path
from queue import Full, Queue
import re
import sqlite3
import ssl
import sys
import threading
import time
from typing import NamedTuple
import unicodedata
from urllib.error import HTTPError
from urllib.request import urlopen

from chinese_text import simplify_chinese
from geoip_local import LocalGeoIP
from ip_location_cache import (
    DAY, LOCATION_FORMAT_VERSION, cache_path, cached_location, open_cache, postpone_requests, reserve_request,
    save_location,
)


CHINESE_REGIONS = {'HK': '香港', 'MO': '澳门', 'TW': '台湾'}
CITY_EQUIVALENT_REGIONS = {'北京', '天津', '上海', '重庆', *CHINESE_REGIONS.values()}
LAN_NETWORKS = tuple(map(ip_network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                                      '169.254.0.0/16', 'fc00::/7', 'fe80::/10')))
MULTICAST_GROUPS = {
    'ff02::16': 'MLDv2 路由器组播',
    'ff02::1:2': 'DHCPv6 中继/服务器组播',
}
PROVINCE_SUFFIX = re.compile(r'(壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市)$')
ENGLISH_SUFFIX = re.compile(r' (?:(?:hui|uygur|uyghur|zhuang) autonomous region|autonomous region|province|municipality)$')
PROVINCES = {
    'anhui': '安徽', 'beijing': '北京', 'chongqing': '重庆', 'fujian': '福建', 'gansu': '甘肃',
    'guangdong': '广东', 'guangxi': '广西', 'guizhou': '贵州', 'hainan': '海南', 'hebei': '河北',
    'heilongjiang': '黑龙江', 'henan': '河南', 'hubei': '湖北', 'hunan': '湖南', 'inner mongolia': '内蒙古',
    'jiangsu': '江苏', 'jiangxi': '江西', 'jilin': '吉林', 'liaoning': '辽宁', 'nei mongol': '内蒙古',
    'ningxia': '宁夏', 'qinghai': '青海', 'shaanxi': '陕西', 'shandong': '山东', 'shanghai': '上海',
    'shanxi': '山西', 'sichuan': '四川', 'tianjin': '天津', 'tibet': '西藏', 'xinjiang': '新疆',
    'xizang': '西藏', 'yunnan': '云南', 'zhejiang': '浙江', 'hong kong': '香港', 'macao': '澳门',
    'macau': '澳门', 'taiwan': '台湾',
}
US_STATES = {
    'alabama': '阿拉巴马州', 'alaska': '阿拉斯加州', 'arizona': '亚利桑那州', 'arkansas': '阿肯色州',
    'california': '加利福尼亚州', 'colorado': '科罗拉多州', 'connecticut': '康涅狄格州', 'delaware': '特拉华州',
    'district of columbia': '华盛顿特区', 'florida': '佛罗里达州', 'georgia': '佐治亚州', 'hawaii': '夏威夷州',
    'idaho': '爱达荷州', 'illinois': '伊利诺伊州', 'indiana': '印第安纳州', 'iowa': '艾奥瓦州',
    'kansas': '堪萨斯州', 'kentucky': '肯塔基州', 'louisiana': '路易斯安那州', 'maine': '缅因州',
    'maryland': '马里兰州', 'massachusetts': '马萨诸塞州', 'michigan': '密歇根州', 'minnesota': '明尼苏达州',
    'mississippi': '密西西比州', 'missouri': '密苏里州', 'montana': '蒙大拿州', 'nebraska': '内布拉斯加州',
    'nevada': '内华达州', 'new hampshire': '新罕布什尔州', 'new jersey': '新泽西州', 'new mexico': '新墨西哥州',
    'new york': '纽约州', 'north carolina': '北卡罗来纳州', 'north dakota': '北达科他州', 'ohio': '俄亥俄州',
    'oklahoma': '俄克拉何马州', 'oregon': '俄勒冈州', 'pennsylvania': '宾夕法尼亚州', 'rhode island': '罗得岛州',
    'south carolina': '南卡罗来纳州', 'south dakota': '南达科他州', 'tennessee': '田纳西州', 'texas': '得克萨斯州',
    'utah': '犹他州', 'vermont': '佛蒙特州', 'virginia': '弗吉尼亚州', 'washington': '华盛顿州',
    'west virginia': '西弗吉尼亚州', 'wisconsin': '威斯康星州', 'wyoming': '怀俄明州',
    '加州': '加利福尼亚州', '德克萨斯州': '得克萨斯州', '哥伦比亚特区': '华盛顿特区',
}
DETAILED_COUNTRIES = {'中国', '美国'}
# DB-IP sometimes appends the district, e.g. "Guangzhou (Yuexiu Qu)".
DISTRICT_SUFFIX = re.compile(r'\s*\([^()]*\)$')
UNKNOWN_PLACES = {'', '-', 'n/a', 'unknown', '未知'}
RETRY_DELAY = 60
RESULT_TTL = 30 * DAY
MISS_TTL = DAY
IPWHO_FIELDS = 'ip,success,country,country_code,region,city,connection.asn,connection.isp,connection.org'


class PeerDetails(NamedTuple):
    location: str = ''
    carrier: str = ''
    asn: str = ''


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    daily_limit: int
    interval: float
    default_cooldown: int
    parse: Callable[[dict], PeerDetails]
    country_only: bool = False
    daily_limit_status: int | None = None


@lru_cache(maxsize=1)
def territories():
    path = Path(__file__).with_name('geo_data') / 'territories.zh.json'
    return json.loads(path.read_text(encoding='utf-8'))


def city_key(city):
    name = unicodedata.normalize('NFKD', city.strip().removesuffix('市')).casefold()
    return ''.join(char for char in name if char.isalnum())


@lru_cache(maxsize=1)
def city_aliases():
    path = Path(__file__).with_name('geo_data') / 'cities.zh.json'
    countries = json.loads(path.read_text(encoding='utf-8'))
    aliases = {}
    for country, regions in countries.items():
        for region, cities in regions.items():
            for city, names in cities.items():
                for name in (city, *names):
                    aliases[country, region, city_key(simplify_chinese(name))] = city
    return aliases


def normalize_city(label):
    label = simplify_chinese(label)
    parts = label.split('·')
    if len(parts) != 3:
        return label
    city, region, country = parts
    canonical = city_aliases().get((country, region, city_key(city)), city)
    return '·'.join((canonical, region, country))


def peer_ip(address):
    ip = ip_address(address)
    return (ip.ipv4_mapped or ip) if ip.version == 6 else ip


@lru_cache(maxsize=4096)
def classify_address(address):
    ip = peer_ip(address)
    if any(ip in network for network in LAN_NETWORKS):
        return '', PeerDetails('局域网')
    if ip.is_multicast:
        group = MULTICAST_GROUPS.get(str(ip).partition('%')[0], f'IPv{ip.version} 组播')
        return '', PeerDetails(group)
    if ip.is_global and not ip.is_reserved:
        return str(ip).partition('%')[0], PeerDetails()
    return '', PeerDetails()


def location_label(code, country, region, city):
    code = code.strip().upper() if isinstance(code, str) else ''
    if len(code) != 2 or not code.isascii() or not code.isalpha() or code == 'ZZ':
        return ''
    if code in CHINESE_REGIONS:
        return CHINESE_REGIONS[code] + '·中国'
    if code in ('CN', 'US'):
        region = simplify_chinese(region.strip()) if isinstance(region, str) else ''
        if code == 'CN':
            country = '中国'
            region = PROVINCE_SUFFIX.sub('', region)
            if region.isascii():
                region = PROVINCES.get(ENGLISH_SUFFIX.sub('', region.lower()), '')
        else:
            country = '美国'
            region = US_STATES.get(region.casefold(), region)
        if region.casefold() in UNKNOWN_PLACES or '·' in region:
            return country
        label = region + '·' + country
        if region not in CITY_EQUIVALENT_REGIONS and isinstance(city, str):
            city = city.strip().removesuffix('市')
            if city.casefold() not in UNKNOWN_PLACES and '·' not in city:
                return normalize_city(city + '·' + label)
        return label
    return territories().get(code, simplify_chinese(country.strip()) if isinstance(country, str) else '')


def asn_label(value):
    match = re.fullmatch(r'(?:AS\s*)?([0-9]{1,10})', str(value).strip(), flags=re.IGNORECASE)
    number = int(match[1]) if match else 0
    return 'AS{}'.format(number) if 0 < number < 2 ** 32 else ''


def peer_details(code, country=None, region=None, *operators, city=None, asn=None):
    label = location_label(code, country, region, city)
    carrier = next((operator.strip() for operator in operators
                    if isinstance(operator, str) and operator.strip() not in ('', '-')), '')
    return PeerDetails(label, simplify_chinese(carrier), asn_label(asn))


def ipwho_details(payload):
    connection = payload.get('connection')
    if not isinstance(connection, dict):
        connection = {}
    return peer_details(payload.get('country_code'), payload.get('country'), payload.get('region'),
                        connection.get('isp'), connection.get('org'),
                        city=payload.get('city'), asn=connection.get('asn'))


def geojs_details(payload):
    return peer_details(payload.get('country_code'), payload.get('country'), payload.get('region'),
                        payload.get('organization_name'), city=payload.get('city'), asn=payload.get('asn'))


def freeipapi_details(payload):
    return peer_details(payload.get('countryCode'), payload.get('countryName'), payload.get('regionName'),
                        payload.get('asnOrganization'), city=payload.get('cityName'), asn=payload.get('asn'))


def ipquery_details(payload):
    location = payload.get('location')
    isp = payload.get('isp')
    if not isinstance(location, dict):
        location = {}
    if not isinstance(isp, dict):
        isp = {}
    return peer_details(location.get('country_code'), location.get('country'), location.get('state'),
                        isp.get('isp'), isp.get('org'), city=location.get('city'), asn=isp.get('asn'))


def ip2location_details(payload):
    return peer_details(payload.get('country_code'), payload.get('country_name'), payload.get('region_name'),
                        payload.get('as'), city=payload.get('city_name'), asn=payload.get('asn'))


def country_is_details(payload):
    return peer_details(payload.get('country'))


def record_name(record, *languages):
    names = record.get('names') if isinstance(record, dict) else None
    if not isinstance(names, dict):
        return ''
    return next((names[language] for language in languages if isinstance(names.get(language), str)), '')


def local_details(records):
    """Convert DB-IP city and ASN records into the same labels the APIs produce."""
    city, network = records
    country = city.get('country') if isinstance(city.get('country'), dict) else {}
    subdivisions = city.get('subdivisions')
    region = subdivisions[0] if isinstance(subdivisions, list) and subdivisions else {}
    # English names match the province, state and city alias tables; Chinese names are fallbacks.
    return peer_details(country.get('iso_code'), record_name(country, 'zh-CN', 'en'),
                        record_name(region, 'en', 'zh-CN'), network.get('autonomous_system_organization'),
                        city=DISTRICT_SUFFIX.sub('', record_name(city.get('city'), 'en', 'zh-CN')),
                        asn=network.get('autonomous_system_number', ''))


def local_geoip(changed):
    return LocalGeoIP(cache_path().parent / 'geoip', changed, tls_context=geo_tls_context(), convert=local_details)


def missing_city(location):
    region, _, country = location.partition('·')
    return country in DETAILED_COUNTRIES and region not in CITY_EQUIVALENT_REGIONS


def prefer_location(current, candidate):
    if not current or (current in DETAILED_COUNTRIES and candidate.endswith('·' + current)):
        return candidate
    if missing_city(current) and candidate.endswith('·' + current):
        return candidate
    return current


PROVIDERS = (
    Provider('ipwho.is', 'https://ipwho.is/{address}?lang=zh-CN&fields=' + IPWHO_FIELDS,
             900, 1, DAY, ipwho_details),
    Provider('GeoJS', 'https://get.geojs.io/v1/ip/geo/{address}.json', 250, 2, 900, geojs_details),
    Provider('FreeIPAPI', 'https://free.freeipapi.com/api/v1/json/{address}', 300, 2, 60, freeipapi_details),
    Provider('IPQuery', 'https://api.ipquery.io/{address}?format=json', 200, 2, 900, ipquery_details),
    Provider('IP2Location.io', 'https://api.ip2location.io/?ip={address}', 900, 2, DAY,
             ip2location_details, daily_limit_status=401),
    Provider('Country.is', 'https://api.country.is/{address}', 150, 2, 300, country_is_details, country_only=True),
)


@lru_cache(maxsize=1)
def geo_tls_context():
    context = ssl.create_default_context()
    if getattr(sys, 'frozen', False):
        # OpenSSL's default paths can refer to the build machine in a standalone bundle.
        context.load_verify_locations(cafile=Path(__file__).with_name('geo_data') / 'cacert.pem')
    return context


def fetch_location(address, provider=PROVIDERS[0]):
    with urlopen(provider.url.format(address=address), timeout=3, context=geo_tls_context()) as response:
        payload = json.loads(response.read(16_384))
    if not isinstance(payload, dict) or payload.get('error'):
        raise ValueError('Invalid IP location response')
    if payload.get('success', True) is not True:
        return PeerDetails()
    returned_address = payload.get('ip', payload.get('ipAddress'))
    if returned_address is not None and ip_address(returned_address) != ip_address(address):
        raise ValueError('IP location response is for another address')
    return provider.parse(payload)


def retry_time(error, now, provider):
    if error.code == provider.daily_limit_status:
        return (int(now) // DAY + 1) * DAY
    if error.code != 429:
        return now + RETRY_DELAY
    header = error.headers.get('Retry-After', '') if error.headers is not None else ''
    try:
        if header.isascii() and header.isdecimal():
            until = now + int(header)
        else:
            until = parsedate_to_datetime(header).timestamp()
    except (ValueError, OverflowError, OSError):
        until = now + provider.default_cooldown
    return max(now + RETRY_DELAY, until)


class IPLocations:
    def __init__(self, changed, cache_file=None, geoip=None):
        self.changed = changed
        self.cache_file = cache_file
        # Local GeoIP data answers first while fresh; the APIs cover gaps and stale data.
        self.geoip = geoip
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.requests = Queue(maxsize=32)
        self.worker = None
        self.cache = {}

    def lookup(self, address):
        address, local = classify_address(address)
        if not address:
            return local
        if self.geoip is not None:
            self.geoip.start()
            details = self.geoip.lookup(address)
            if details is not None and details.location:
                return details
        details = self._lookup_online(address)
        if not any(details) and self.geoip is not None:
            # Outdated local data is still better than nothing while the APIs have no answer.
            stale = self.geoip.lookup(address, stale=True)
            if stale is not None:
                return stale
        return details

    def _lookup_online(self, address):
        with self.lock:
            details, retry_at = self.cache.get(address, (PeerDetails(), 0))
            if self.closed.is_set() or time.time() < retry_at:
                return details
            try:
                self.requests.put_nowait(address)
            except Full:
                return details
            self.cache[address] = details, float('inf')
            if self.worker is None:
                self.worker = threading.Thread(target=self._resolve, daemon=True)
                self.worker.start()
            return details

    def _query(self, database, address):
        retries = []
        location, carrier, asn = '', '', ''
        for provider in PROVIDERS:
            if provider.country_only and location:
                continue
            while not self.closed.is_set():
                now = time.time()
                eligible, pacing = reserve_request(database, provider, now)
                if not pacing:
                    break
                self.closed.wait(max(0, eligible - time.time()))
            if self.closed.is_set():
                return PeerDetails(location, carrier, asn), time.time()
            if eligible > now:
                retries.append(eligible)
                continue
            try:
                details = fetch_location(address, provider)
            except HTTPError as error:
                with error:
                    until = retry_time(error, time.time(), provider)
                postpone_requests(database, provider, until)
                retries.append(until)
            except (OSError, ValueError, HTTPException):
                until = time.time() + RETRY_DELAY
                postpone_requests(database, provider, until)
                retries.append(until)
            else:
                location = prefer_location(location, details.location)
                carrier = carrier or details.carrier
                asn = asn or details.asn
                if location and location not in DETAILED_COUNTRIES and carrier and not missing_city(location):
                    return PeerDetails(location, carrier, asn), time.time() + RESULT_TTL
        ttl = RESULT_TTL if location and location not in DETAILED_COUNTRIES else MISS_TTL
        if carrier and missing_city(location):
            return PeerDetails(location, carrier, asn), time.time() + ttl
        return PeerDetails(location, carrier, asn), min(retries, default=time.time() + ttl)

    def _cached_or_query(self, database, address):
        cached = cached_location(database, address, time.time())
        if cached is not None:
            location, carrier, expires, format_version, asn = cached
            location = normalize_city(location)
            previous = PeerDetails(location, simplify_chinese(carrier or ''), asn or '')
            country = location.rsplit('·', 1)[-1]
            required_version = LOCATION_FORMAT_VERSION if country == '美国' else 1
            needs_details = format_version < required_version and (
                location in DETAILED_COUNTRIES or missing_city(location)
            )
            if not any(previous) or (carrier is not None and asn is not None and not needs_details):
                return previous, expires
            with self.lock:
                self.cache[address] = previous, float('inf')
            self.changed.set()

        details, expires = self._query(database, address)
        if cached is not None:
            if not any(details):
                return previous, min(cached[2], expires)
            details = PeerDetails(prefer_location(details.location, previous.location),
                                  details.carrier or previous.carrier, details.asn or previous.asn)
        if not self.closed.is_set():
            save_location(database, address, *details, expires, time.time())
        return details, expires

    def _resolve(self):
        with ExitStack() as resources:
            database = None
            while not self.closed.is_set():
                address = self.requests.get()
                if address is None or self.closed.is_set():
                    return
                try:
                    if database is None:
                        database = resources.enter_context(open_cache(self.cache_file))
                    result = self._cached_or_query(database, address)
                except (OSError, sqlite3.Error):
                    with self.lock:
                        details = self.cache[address][0]
                    result = details, time.time() + RETRY_DELAY
                with self.lock:
                    self.cache[address] = result
                if any(result[0]):
                    self.changed.set()

    def close(self):
        self.closed.set()
        if self.geoip is not None:
            self.geoip.close()
        if self.worker is not None:
            try:
                self.requests.put_nowait(None)
            except Full:
                pass
