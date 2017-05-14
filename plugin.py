###
# Copyright (c) 2014, Mike Stewart
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###

from __future__ import division

import supybot.utils as utils
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.schedule as schedule
import supybot.ircmsgs as ircmsgs
import supybot.log as log

import datetime
import json
import random
import re
import time
import urllib2

import feedparser
import dateutil.parser
from html2text import HTML2Text
from BeautifulSoup import BeautifulSoup
from dateutil.relativedelta import relativedelta

DEVLOG_URL = 'http://www.bay12games.com/dwarves/dev_now.rss'
RELEASE_LOG_URL = 'http://www.bay12games.com/dwarves/dev_release.rss'
CHANGELOG_URL = 'http://www.bay12games.com/dwarves/mantisbt/changelog_page.php'

DATE_FORMAT = '%B %d, %Y'

def pluralize(n, word, ending='s'):
    return '%i %s%s' % (n, word, ending if n != 1 else '')

_default_timezone = datetime.tzinfo(0)
def relativedelta_string(t1, t2=None):
    if t2 is None:
        t1, t2 = datetime.datetime.utcnow(), t1
    t1, t2 = t1.replace(tzinfo=_default_timezone), t2.replace(tzinfo=_default_timezone)
    delta = relativedelta(t1, t2)
    delta_str = ''
    if delta.years:
        delta_str += pluralize(delta.years, 'year') + ', '
    if delta.months:
        delta_str += pluralize(delta.months, 'month') + ', '
    if delta.days:
        delta_str += pluralize(delta.days, 'day')
    delta_str = delta_str.rstrip(', ')
    if not delta_str:
        delta_str = 'today'
    else:
        delta_str += ' ago'
    return delta_str

class CacheEntry(object):
    def __init__(self, contents, life=3600):
        self.contents = contents
        self.time = time.time()
        self.life = max(0, life)

    @property
    def expired(self):
        return time.time() > self.time + self.life

class GithubApi(object):
    def __init__(self):
        self._cache = {}

    def request(self, url, ignore_cache=False):
        if not url.startswith('https://'):
            url = 'https://api.github.com/' + url
        if url not in self._cache or self._cache[url].expired or ignore_cache:
            self._cache[url] = CacheEntry(json.load(urllib2.urlopen(url)))
        return self._cache[url].contents

    def clear_cache(self):
        self._cache.clear()

ghapi = GithubApi()

class GSearchNoResults(Exception): pass

def gsearch_clean(x):
    return ' %s ' % re.sub(r'[^a-z0-9]', '', str(x).lower())

def gsearch_relevance(search, candidate):
    """ Return the relevance of a candidate - 0 = perfect match, -1 = invalid """
    search, candidate = gsearch_clean(search), gsearch_clean(candidate)
    relevance = 0
    while search:
        pos = candidate.find(search[0])
        if pos == -1:
            return -1
        relevance += pos
        search = search[1:]
        candidate = candidate[pos+1:]
    return relevance

def gsearch(search, candidates, candidate_filter=lambda result: result):
    results = candidates[:]
    results.sort(key=lambda c: gsearch_relevance(search, candidate_filter(c)))
    while results and gsearch_relevance(search, candidate_filter(results[0])) == -1:
        results.pop(0)
    return results

def gsearch_top(*args, **kwargs):
    results = gsearch(*args, **kwargs)
    if results:
        return results[0]
    else:
        raise GSearchNoResults

class DFBugMonitor(callbacks.Plugin):
    """Simply load the plugin, and it will periodically check for DF bugfixes
    and announce them"""

    def __init__(self, irc):
        self.__parent = super(DFBugMonitor, self)
        self.__parent.__init__(irc)

        self.irc = irc

        # Get the latest devlog
        d = feedparser.parse(DEVLOG_URL)
        self.last_devlog = d.entries[0].title

        # Prepare the already-known-issues set
        self.known_issues = set()
        self.first_run = True

        # Find the latest version
        soup = BeautifulSoup(urllib2.urlopen(CHANGELOG_URL).read())

        latest_version_link = soup('tt')[0].findAll('a')[1]
        matches = re.search('\d+$', latest_version_link['href'])
        self.version_id = int(matches.group(0))

        matches = re.search('^[\d\.]+$', latest_version_link.text)
        if matches:
            # The latest listed version has already been released, so our
            # target version ID is probably one more
            self.version_id = self.version_id + 1

        print 'Starting at version %u' % (self.version_id,)

        self.schedule_event(self.scrape_changelog, 'bug_poll_s', 'scrape')
        self.schedule_event(self.check_devlog, 'devlog_poll_s', 'check_devlog')

    def schedule_event(self, f, config_value, name):
        # Like schedule.addPeriodicEvent, but capture the name of our config
        # variable in the closure rather than the value
        if name in schedule.schedule.events:
            log.warning('Event %s already scheduled; removing' % name)
            schedule.removeEvent(name)
        def wrapper():
            try:
                f()
            finally:
                return schedule.addEvent(wrapper, time.time() + self.registryValue(config_value), name)

        return wrapper()

    def check_devlog(self):
        d = feedparser.parse(DEVLOG_URL)

        date = d.entries[0].title

        if date != self.last_devlog:
            # New devlog!
            self.last_devlog = date

            title = ircutils.bold('%s %s: ' % (d.feed.title, date))
            summary = d.entries[0].summary
            full_message = title + summary

            # Parse and wrap the message with html2text
            h = HTML2Text()
            h.body_width = self.registryValue('max_chars_per_line')

            # Convert the message to text, and strip empty lines
            processed_message = h.handle(full_message)
            split_message = filter(None, [x.strip() for x in processed_message.split('\n')])

            max_lines = self.registryValue('max_lines')
            if len(split_message) > max_lines:
                # The devlog is too long... give a configured number and a link
                devlog_url = d.entries[0].id

                split_message = split_message[0:max_lines]
                split_message.append('... ' + devlog_url)

            self.queue_messages(split_message)

    def scrape_changelog(self):
        changelog_url = CHANGELOG_URL+('?version_id=%u' % (self.version_id,))
        soup = BeautifulSoup(urllib2.urlopen(changelog_url).read(),
                convertEntities=BeautifulSoup.HTML_ENTITIES)

        # First check to make sure the version name hasn't changed on us
        version_name = soup('tt')[0].findAll('a')[1].text

        matches = re.search('^[\d\.]+$', version_name)
        if matches:
            # New version incoming!
            self.queue_messages([ircutils.bold('Dwarf Fortress v%s has been released!' % (version_name,))])

            # Prepare for the next version
            self.version_id = self.version_id + 1
            self.known_issues.clear()
            return


        # Prepare a list of messages to be sent
        msg_list = []

        # Base our scrape off of the br tags that separate issues
        lines = soup('tt')[0].findAll('br')

        for i in range(2, len(lines)):
            issue = lines[i]

            # Extract the issue ID from the link to the issue
            issue_id_link = issue.findNext('a')
            issue_id = issue_id_link.text

            if issue_id in self.known_issues:
                continue

            # Start by adding the issue to the list of known issues for this
            # version
            self.known_issues.add(issue_id)

            if self.first_run:
                # If this is the first run, just fill out the known issues set
                # but don't send any messages
                continue

            # Get the URL of the bug page
            issue_url = 'http://www.bay12games.com' + issue_id_link['href']

            # Grab the bolded category, and use it to find the description
            issue_category_b = issue.findNext('b')
            issue_category = issue_category_b.text
            issue_title = issue_category_b.nextSibling

            # Get the link to the fix author (probable Toady) for their name and
            # the resolution status
            issue_fixer_link = issue.findNext('a', {'class': None})
            issue_fixer = issue_fixer_link.text
            issue_status = issue_fixer_link.nextSibling

            # Build up the formatted message to send, and add it to the list
            bolded_id_and_category = ircutils.bold('%s: %s' % (issue_id,
                issue_category))
            msg_list.append('%s %s%s%s ( %s )' % (bolded_id_and_category,
                    issue_title, issue_fixer, issue_status, issue_url))

            # Get the closing note and add it to the list as well
            last_note_msg = self.get_closing_note(issue_url)
            if last_note_msg:
                msg_list.append(last_note_msg)

        # Now that we've processed all the issues, send out the messages
        if msg_list:
            self.queue_messages(msg_list)

        # Allow messages to be sent next time, if they were inhibited this time
        self.first_run = False

    def get_closing_note(self, issue_url):
        # Read the issue page to check for a closing note by Toady
        soup = BeautifulSoup(urllib2.urlopen(issue_url).read())
        bug_notes = soup.findAll('tr', 'bugnote')

        if not bug_notes:
            # No bug notes
            return []

        # Check the last note on the page to see who made it
        last_note = bug_notes[-1]
        last_note_author = last_note.findAll('a')[1].text

        if last_note_author == u'Toady One':
            # Grab Toady's last note on the bug
            last_note_msg = '"' + last_note.findNext('td',
                    'bugnote-note-public').text + '"'
            return last_note_msg
        else:
            # Last not wasn't from Toady
            return []

    def queue_messages(self, msg_list):
        for channel in sorted(self.irc.state.channels):
            for msg in msg_list:
                self.irc.queueMsg(ircmsgs.privmsg(channel, msg))

    class df(callbacks.Commands):
        def version(self, irc, msg, args):
            """takes no arguments

            Returns the current DF version
            """
            e = feedparser.parse(RELEASE_LOG_URL).entries[0]
            version = re.search(r'DF\s*(\S+)', e.title).group(1)
            date = time.strftime(DATE_FORMAT, e.published_parsed)

            t = datetime.datetime.fromtimestamp(time.mktime(e.published_parsed))
            delta_str = relativedelta_string(t)

            irc.reply('Latest DF version: %s, released %s [%s]: http://www.bay12games.com/dwarves/' % (version, date, delta_str))
        version = wrap(version)

    class dfhack(callbacks.Commands):
        def version(self, irc, msg, args):
            """takes no arguments

            Returns the current DFHack version
            """
            rel = ghapi.request('repos/dfhack/dfhack/releases')[0]
            publish_date = dateutil.parser.parse(rel['published_at'])
            date = time.strftime(DATE_FORMAT, publish_date.timetuple())
            irc.reply('Latest DFHack version: %s, released by %s on %s [%s]: %s' % (
                rel['tag_name'],
                rel['author']['login'],
                date,
                relativedelta_string(publish_date),
                rel['html_url']
            ))
        version = wrap(version)

        def downloads(self, irc, msg, args, release_name):
            """[<release>]

            Returns download statistics for the given or latest release
            """
            stat_format = '%s: %i/%i (%.1f%%)'

            try:
                if release_name:
                    info = ghapi.request('repos/dfhack/dfhack/releases/tags/%s' % release_name.strip())
                else:
                    info = ghapi.request('repos/dfhack/dfhack/releases')[0]
                    release_name = info['tag_name']
            except urllib2.HTTPError as e:
                irc.reply('Could not fetch release information - nonexistent release? (%s)' % e)
                return

            if not len(info['assets']):
                irc.reply('No downloads for DFHack %s' % release_name)
                return

            os_counts = {'Linux': 0, 'OS X': 0, 'Windows': 0, '*': 0}
            file_counts = {'*': 0}
            def inc_os_count(key, value):
                os_counts[key] += value
                os_counts['*'] += value
            for asset in info['assets']:
                name = re.sub(r'dfhack|\.tar|\.bz2|\.gz|\.xz|\.7z|\.zip', '', asset['name'], flags=re.I).replace(release_name, '').strip('-')
                file_counts[name] = asset['download_count']
                file_counts['*'] += asset['download_count']
                clean_name = re.sub(r'[^A-Za-z]', '', name.lower())
                if 'linux' in clean_name:
                    inc_os_count('Linux', asset['download_count'])
                elif 'windows' in clean_name:
                    inc_os_count('Windows', asset['download_count'])
                elif 'osx' in clean_name or 'mac' in clean_name or 'darwin' in clean_name:
                    inc_os_count('OS X', asset['download_count'])
            irc.reply(('Stats for %s: ' % release_name) +
                ' | '.join(stat_format % (os, num, os_counts['*'], (num/os_counts['*'] * 100) if os_counts['*'] else 0)
                    for os, num in os_counts.items() if os != '*'))

            messages = list(stat_format % (file, num, file_counts['*'], (num/file_counts['*'] * 100) if os_counts['*'] else 0)
                for file, num in file_counts.items() if file != '*')
            current_message = ''
            while True:
                if not len(messages):
                    irc.reply(current_message.rstrip('| '))
                    break
                msg = messages.pop()
                if len(current_message) + len(msg) + 3 > 400:
                    irc.reply(current_message.rstrip('| '))
                    current_message = ''
                current_message += msg + ' | '

        downloads = wrap(downloads, [optional('text')])

        def todo(self, irc, msg, args):
            """takes no arguments"""

            milestones = ghapi.request('https://api.github.com/repos/dfhack/dfhack/milestones?state=open')
            milestones.sort(key=lambda m: m['number'])
            if not len(milestones):
                irc.reply('No open milestones')
                return
            cur_release = ghapi.request('repos/dfhack/dfhack/releases')[0]['tag_name']
            release_regex = re.compile(r'(r)(\d+)$')
            match = release_regex.search(cur_release)
            next_milestone = None

            if match:
                next_release = release_regex.sub(match.group(1) + str(int(match.group(2)) + 1), cur_release)
                next_milestone = None
                def find_next_milestone(callback):
                    candidates = filter(callback, milestones)
                    candidates = filter(lambda m: m['open_issues'] or m['closed_issues'], candidates)
                    candidates = list(candidates)
                    return candidates[0] if len(candidates) else None

                next_milestone = find_next_milestone(lambda m: m['title'] == next_release)
                if not next_milestone:
                    next_milestone = find_next_milestone(lambda m: 'next' in m['description'].lower())

            if not next_milestone:
                if milestones:
                    next_milestone = milestones[0]
                else:
                    irc.reply('Could not find milestone following %s' % cur_release)
                    return

            closed = next_milestone['closed_issues']
            total = next_milestone['open_issues'] + closed
            irc.reply('Next milestone: %s | %i/%i items done (%.1f%%) | %s' %
                (next_milestone['title'], closed, total, closed/total * 100, next_milestone['html_url']))

        todo = wrap(todo)

        def get(self, irc, msg, args, filename):
            """[<part of filename>]"""
            info = ghapi.request('repos/dfhack/dfhack/releases')[0]
            def send_valid_assets():
                irc.reply('Available downloads: %s' %
                    ', '.join(map(lambda a: re.sub(r'\-*dfhack\-*', '', a['name']), info['assets'])))
            if not filename:
                send_valid_assets()
                irc.reply('Use "dfhack get <part of filename>" for a direct link')
                return
            try:
                asset = gsearch_top(filename, info['assets'], lambda a: a['name'])
                irc.reply('%s: %s' % (asset['name'], asset['browser_download_url']))
            except GSearchNoResults:
                irc.reply('No downloads matching "%s" found' % filename)
                send_valid_assets()

        get = wrap(get, [optional('text')])

    class github(callbacks.Commands):

        def ratelimit(self, irc, msg, args):
            """takes no arguments"""
            rate = ghapi.request('rate_limit', ignore_cache=True)['rate']
            secs = int(rate['reset'] - time.time())
            irc.reply('%i/%i remaining; resets in %i:%02i' %
                (rate['remaining'], rate['limit'], secs // 60, secs % 60))

        ratelimit = wrap(ratelimit)

        def refresh(self, irc, msg, args):
            """takes no arguments"""
            ghapi.clear_cache()

        refresh = wrap(refresh, [('checkCapability', 'admin')])

    def die(self):
        schedule.removeEvent('scrape')
        schedule.removeEvent('check_devlog')


Class = DFBugMonitor


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
