# ex:ts=4:sw=4:sts=4:et
# -*- tab-width: 4; c-basic-offset: 4; indent-tabs-mode: nil -*-
from __future__ import absolute_import
import re
import os
import xml.etree.ElementTree as ET
import json
import copy

from svtplay_dl.utils.urllib import urlparse, parse_qs, quote_plus, Cookie
from svtplay_dl.service import Service, OpenGraphThumbMixin
from svtplay_dl.utils import is_py2_old, filenamify, CookieJar
from svtplay_dl.log import log
from svtplay_dl.fetcher.hls import hlsparse, HLS
from svtplay_dl.fetcher.rtmp import RTMP
from svtplay_dl.fetcher.hds import hdsparse
from svtplay_dl.subtitle import subtitle

class Tv4play(Service, OpenGraphThumbMixin):
    supported_domains = ['tv4play.se', 'tv4.se']

    def __init__(self, url):
        Service.__init__(self, url)
        self.subtitle = None
        self.cookies = {}

    def get(self, options):
        data = self.get_urldata()

        vid = findvid(self.url, data)
        if vid is None:
            log.error("Can't find video id for %s", self.url)
            return

        if options.username and options.password:
            data = self.http.get("https://www.tv4play.se/session/new?https=")
            auth_token = re.search('name="authenticity_token" ([a-z]+="[^"]+" )?value="([^"]+)"', data.text)
            if not auth_token:
                log.error("Can't find authenticity_token needed for user / password")
                return
            url = "https://www.tv4play.se/session"
            postdata = {"user_name" : options.username, "password": options.password, "authenticity_token":auth_token.group(2), "https": ""}
            data = self.http.post(url, data=postdata, cookies=self.cookies)
            self.cookies = data.cookies
            fail = re.search("<p class='failed-login'>([^<]+)</p>", data.text)
            if fail:
                log.error(fail.group(1))
                return
        url = "http://premium.tv4play.se/api/web/asset/%s/play" % vid
        data = self.http.get(url, cookies=self.cookies)
        if data.status_code == 401:
            xml = ET.XML(data.content)
            code = xml.find("code").text
            if code == "SESSION_NOT_AUTHENTICATED":
                log.error("Can't access premium content")
            elif code == "ASSET_PLAYBACK_INVALID_GEO_LOCATION":
                log.error("Can't downoad this video because of geoblocked.")
            else:
                log.error("Can't find any info for that video")
            return
        if data.status_code == 404:
            log.error("Can't find the video")
            return
        xml = ET.XML(data.content)
        ss = xml.find("items")
        if is_py2_old:
            sa = list(ss.getiterator("item"))
        else:
            sa = list(ss.iter("item"))

        if xml.find("live").text:
            if xml.find("live").text != "false":
                options.live = True
        if xml.find("drmProtected").text == "true":
            log.error("We cant download DRM protected content from this site.")
            return

        if options.output_auto:
            directory = os.path.dirname(options.output)
            options.service = "tv4play"
            title = "%s-%s-%s" % (options.output, vid, options.service)
            title = filenamify(title)
            if len(directory):
                options.output = os.path.join(directory, title)
            else:
                options.output = title

        if self.exclude(options):
            return

        for i in sa:
            if i.find("mediaFormat").text == "mp4":
                base = urlparse(i.find("base").text)
                parse = urlparse(i.find("url").text)
                if "rtmp" in base.scheme:
                    swf = "http://www.tv4play.se/flash/tv4playflashlets.swf"
                    options.other = "-W %s -y %s" % (swf, i.find("url").text)
                    yield RTMP(copy.copy(options), i.find("base").text, i.find("bitrate").text)
                elif parse.path[len(parse.path)-3:len(parse.path)] == "f4m":
                    streams = hdsparse(copy.copy(options), self.http.get(i.find("url").text, params={"hdcore": "3.7.0"}).text, i.find("url").text)
                    if streams:
                        for n in list(streams.keys()):
                            yield streams[n]
            elif i.find("mediaFormat").text == "smi":
                yield subtitle(copy.copy(options), "smi", i.find("url").text)

        url = "http://premium.tv4play.se/api/web/asset/%s/play?protocol=hls" % vid
        data = self.http.get(url, cookies=self.cookies).content
        xml = ET.XML(data)
        ss = xml.find("items")
        if is_py2_old:
            sa = list(ss.getiterator("item"))
        else:
            sa = list(ss.iter("item"))
        for i in sa:
            if i.find("mediaFormat").text == "mp4":
                parse = urlparse(i.find("url").text)
                if parse.path.endswith("m3u8"):
                    streams = hlsparse(i.find("url").text, self.http.get(i.find("url").text).text)
                    for n in list(streams.keys()):
                        yield HLS(copy.copy(options), streams[n], n)

    def find_all_episodes(self, options):
        parse = urlparse(self.url)
        show = parse.path[parse.path.find("/", 1)+1:]
        if not re.search("%", show):
            show = quote_plus(show)
        data = self.http.get("http://webapi.tv4play.se/play/video_assets?type=episode&is_live=false&platform=web&node_nids=%s&per_page=99999" % show)
        jsondata = json.loads(data)
        episodes = []
        n = 1
        for i in jsondata["results"]:
            try:
                days = int(i["availability"]["availability_group_free"])
            except (ValueError, TypeError):
                days = 999
            if days > 0:
                video_id = i["id"]
                url = "http://www.tv4play.se/program/%s?video_id=%s" % (
                    show, video_id)
                episodes.append(url)
                if n == options.all_last:
                    break
                n += 1

        return episodes

def findvid(url, data):
    parse = urlparse(url)
    if "tv4play.se" in url:
        try:
            vid = parse_qs(parse.query)["video_id"][0]
        except KeyError:
            return None
    else:
        match = re.search(r"\"vid\":\"(\d+)\",", data)
        if match:
            vid = match.group(1)
        else:
            match = re.search(r"-(\d+)$", url)
            if match:
                vid = match.group(1)
            else:
                match = re.search(r"meta content='([^']+)' property='og:video'", data)
                if match:
                    match = re.search(r"vid=(\d+)&", match.group(1))
                    if match:
                        vid = match.group(1)
                    else:
                        log.error("Can't find video id for %s", url)
                        return
                else:
                    return None
    return vid