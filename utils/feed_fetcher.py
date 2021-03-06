import time
import datetime
import traceback
import multiprocessing
import urllib2
import xml.sax
import redis
import random
import pymongo
from django.conf import settings
from django.db import IntegrityError
from django.core.cache import cache
from apps.rss_feeds.models import Feed, MStory
from apps.statistics.models import MAnalyticsFetcher
from utils import feedparser
from utils.story_functions import pre_process_story, strip_tags
from utils import log as logging
from utils.feed_functions import timelimit, TimeoutError, utf8encode, cache_bust_url


FEED_OK, FEED_SAME, FEED_ERRPARSE, FEED_ERRHTTP, FEED_ERREXC = range(5)

import sys
reload(sys)
sys.setdefaultencoding('utf-8')

def mtime(ttime):
    """ datetime auxiliar function.
    """
    return datetime.datetime.fromtimestamp(time.mktime(ttime))
    

# According to the feed_address, FetchFeed uses feedparser to 
# download the feed. and return a dict about the feed.
class FetchFeed:
    def __init__(self, feed_id, options):
        self.feed = Feed.get_by_id(feed_id)
        self.options = options
        self.fpf = None
    
    @timelimit(150)
    def fetch(self):
        """     
        Uses feedparser to download the feed. Will be parsed later.
        """
        start = time.time()
        identity = self.get_identity()
        log_msg = u'%2s ---> [%-30s] ~FYFetching feed (~FB%d~FY)' % (identity,
                                                            self.feed.title[:30],
                                                            self.feed.id)
        logging.debug(log_msg)
                                                 
        etag = self.feed.etag
        modified = self.feed.last_modified.utctimetuple()[:7] if self.feed.last_modified else None
        address = self.feed.feed_address
        
        # If is forced or random is less than 1%, set modified = None and etag = None,
        # means it will fetch new
        if (self.options.get('force') or random.random() <= .01):
            modified = None
            etag = None
            address = cache_bust_url(address)
            logging.debug(u'   ---> [%-30s] ~FBForcing fetch: %s' % (
                          self.feed.title[:30], address))
        # If this feed_id in not fetched once before or not known_good
        elif (not self.feed.fetched_once or not self.feed.known_good):
            modified = None
            etag = None
        
        USER_AGENT = ('NewsBlur Feed Fetcher - %s '
                      '(Mozilla/5.0 (Macintosh; Intel Mac OS X 10_7_1) '
                      'AppleWebKit/534.48.3 (KHTML, like Gecko) Version/5.1 '
                      'Safari/534.48.3)' % (
                          self.feed.permalink,
                     ))

        try:
            self.fpf = feedparser.parse(address,
                                        agent=USER_AGENT,
                                        etag=etag,
                                        modified=modified)
        except (TypeError, ValueError, KeyError), e:
            logging.debug(u'   ***> [%-30s] ~FR%s, turning off headers.' % 
                          (self.feed.title[:30], e))
            self.fpf = feedparser.parse(address, agent=USER_AGENT)
        except (TypeError, ValueError, KeyError, EOFError), e:
            logging.debug(u'   ***> [%-30s] ~FR%s fetch failed: %s.' % 
                          (self.feed.title[:30], e))
            return FEED_ERRHTTP, None
            
        logging.debug(u'   ---> [%-30s] ~FYFeed fetch in ~FM%.4ss' % (
                      self.feed.title[:30], time.time() - start))

        return FEED_OK, self.fpf
        
    def get_identity(self):
        identity = "X"

        current_process = multiprocessing.current_process()
        if current_process._identity:
            identity = current_process._identity[0]

        return identity

        
class ProcessFeed:
    def __init__(self, feed_id, fpf, options):
        self.feed_id = feed_id
        self.options = options
        self.fpf = fpf
    
    def refresh_feed(self):
        self.feed = Feed.get_by_id(self.feed_id)
        if self.feed_id != self.feed.pk:
            logging.debug(" ***> Feed has changed: from %s to %s" % (self.feed_id, self.feed.pk))
            self.feed_id = self.feed.pk
    
    def process(self):
        """ Downloads and parses a feed.
        """
        start = time.time()
        self.refresh_feed()
        
        ret_values = dict(new=0, updated=0, same=0, error=0)

        if hasattr(self.fpf, 'status'):
            if self.options['verbose']:
                if self.fpf.bozo and self.fpf.status != 304:
                    logging.debug(u'   ---> [%-30s] ~FRBOZO exception: %s ~SB(%s entries)' % (
                                  self.feed.title[:30],
                                  self.fpf.bozo_exception,
                                  len(self.fpf.entries)))
                    
            if self.fpf.status == 304:                  # 304 stands for resource not modified
                self.feed = self.feed.save()
                self.feed.save_feed_history(304, "Not modified")
                return FEED_SAME, ret_values
            
            # 302: Temporary redirect: ignore
            # 301: Permanent redirect: save it
            if self.fpf.status == 301:
                if not self.fpf.href.endswith('feedburner.com/atom.xml'):
                    self.feed.feed_address = self.fpf.href
                if not self.feed.known_good:
                    self.feed.fetched_once = True
                    logging.debug("   ---> [%-30s] ~SB~SK~FRFeed is %s'ing. Refetching..." % (self.feed.title[:30], self.fpf.status))
                    self.feed = self.feed.schedule_feed_fetch_immediately()
                if not self.fpf.entries:
                    self.feed = self.feed.save()
                    self.feed.save_feed_history(self.fpf.status, "HTTP Redirect")
                    return FEED_ERRHTTP, ret_values
            if self.fpf.status >= 400:
                logging.debug("   ---> [%-30s] ~SB~FRHTTP Status code: %s. Checking address..." % (self.feed.title[:30], self.fpf.status))
                fixed_feed = None
                if not self.feed.known_good:
                    fixed_feed, feed = self.feed.check_feed_link_for_feed_address()
                if not fixed_feed:
                    self.feed.save_feed_history(self.fpf.status, "HTTP Error")
                else:
                    self.feed = feed
                self.feed = self.feed.save()
                return FEED_ERRHTTP, ret_values

        if not self.fpf.entries:
            if self.fpf.bozo and isinstance(self.fpf.bozo_exception, feedparser.NonXMLContentType):
                logging.debug("   ---> [%-30s] ~SB~FRFeed is Non-XML. %s entries. Checking address..." % (self.feed.title[:30], len(self.fpf.entries)))
                fixed_feed = None
                if not self.feed.known_good:
                    fixed_feed, feed = self.feed.check_feed_link_for_feed_address()
                if not fixed_feed:
                    self.feed.save_feed_history(552, 'Non-xml feed', self.fpf.bozo_exception)
                else:
                    self.feed = feed
                self.feed = self.feed.save()
                return FEED_ERRPARSE, ret_values
            elif self.fpf.bozo and isinstance(self.fpf.bozo_exception, xml.sax._exceptions.SAXException):
                logging.debug("   ---> [%-30s] ~SB~FRFeed has SAX/XML parsing issues. %s entries. Checking address..." % (self.feed.title[:30], len(self.fpf.entries)))
                fixed_feed = None
                if not self.feed.known_good:
                    fixed_feed, feed = self.feed.check_feed_link_for_feed_address()
                if not fixed_feed:
                    self.feed.save_feed_history(553, 'SAX Exception', self.fpf.bozo_exception)
                else:
                    self.feed = feed
                self.feed = self.feed.save()
                return FEED_ERRPARSE, ret_values
                
        # the feed has changed (or it is the first time we parse it)
        # saving the etag and last_modified fields
        self.feed.etag = self.fpf.get('etag')
        if self.feed.etag:
            self.feed.etag = self.feed.etag[:255]
        # some times this is None (it never should) *sigh*
        if self.feed.etag is None:
            self.feed.etag = ''

        try:
            self.feed.last_modified = mtime(self.fpf.modified)
        except:
            self.feed.last_modified = None
            pass
        
        self.fpf.entries = self.fpf.entries[:100]
        
        if self.fpf.feed.get('title'):
            self.feed.feed_title = strip_tags(self.fpf.feed.get('title'))

        self.feed.feed_link = self.fpf.feed.get('link') or self.fpf.feed.get('id') or self.feed.feed_link
        
        self.feed = self.feed.save()

        # Determine if stories aren't valid and replace broken guids
        # if guid is single among many entries:
        #   if permalink also is single among many entries:
        #       replace the guid with published
        #   else if permalink is not:
        #       replace the guid with permalink
        guids_seen = set()
        permalinks_seen = set()
        for entry in self.fpf.entries:
            guids_seen.add(entry.get('guid'))
            permalinks_seen.add(Feed.get_permalink(entry))
        guid_difference = len(guids_seen) != len(self.fpf.entries) # means guid is duplicated.
        single_guid = len(guids_seen) == 1
        replace_guids = single_guid and guid_difference # means guid is single but entries not.
        permalink_difference = len(permalinks_seen) != len(self.fpf.entries)
        single_permalink = len(permalinks_seen) == 1
        replace_permalinks = single_permalink and permalink_difference
        
        # Compare new stories to existing stories, adding and updating
        start_date = datetime.datetime.utcnow()
        story_hashes = []
        stories = []
        for entry in self.fpf.entries:
            story = pre_process_story(entry)
            if story.get('published') < start_date:
                start_date = story.get('published')
            if replace_guids:
                if replace_permalinks:
                    new_story_guid = unicode(story.get('published'))
                    if self.options['verbose']:
                        logging.debug(u'   ---> [%-30s] ~FBReplacing guid (%s) with timestamp: %s' % (
                                      self.feed.title[:30],
                                      story.get('guid'), new_story_guid))
                    story['guid'] = new_story_guid
                else:
                    new_story_guid = Feed.get_permalink(story)
                    if self.options['verbose']:
                        logging.debug(u'   ---> [%-30s] ~FBReplacing guid (%s) with permalink: %s' % (
                                      self.feed.title[:30],
                                      story.get('guid'), new_story_guid))
                    story['guid'] = new_story_guid
            story['story_hash'] = MStory.feed_guid_hash_unsaved(self.feed.pk, story.get('guid'))
            stories.append(story)
            story_hashes.append(story.get('story_hash'))

        # find the existing_stories with story_hash in story_hashes.
        existing_stories = dict((s.story_hash, s) for s in MStory.objects(
            story_hash__in=story_hashes,
            # story_date__gte=start_date,
            # story_feed_id=self.feed.pk
        ))
        
        ret_values = self.feed.add_update_stories(stories, existing_stories,
                                                  verbose=self.options['verbose'],)
        
        logging.debug(u'   ---> [%-30s] ~FYParsed Feed: %snew=%s~SN~FY %sup=%s~SN same=%s%s~SN %serr=%s~SN~FY total=~SB%s' % (
                      self.feed.title[:30], 
                      '~FG~SB' if ret_values['new'] else '', ret_values['new'],
                      '~FY~SB' if ret_values['updated'] else '', ret_values['updated'],
                      '~SB' if ret_values['same'] else '', ret_values['same'],
                      '~FR~SB' if ret_values['error'] else '', ret_values['error'],
                      len(self.fpf.entries)))

        # If there is new story, update all statistics
        self.feed.update_all_statistics(full=bool(ret_values['new']))

        self.feed.save_feed_history(200, "OK")
        
        if self.options['verbose']:
            logging.debug(u'   ---> [%-30s] ~FBTIME: feed parse in ~FM%.4ss' % (
                          self.feed.title[:30], time.time() - start))
        
        return FEED_OK, ret_values


class Dispatcher:
    def __init__(self, options, num_threads):
        self.options = options
        self.feed_stats = {
            FEED_OK:0,
            FEED_SAME:0,
            FEED_ERRPARSE:0,
            FEED_ERRHTTP:0,
            FEED_ERREXC:0}
        self.feed_trans = {
            FEED_OK:'ok',
            FEED_SAME:'unchanged',
            FEED_ERRPARSE:'cant_parse',
            FEED_ERRHTTP:'http_error',
            FEED_ERREXC:'exception'}
        self.feed_keys = sorted(self.feed_trans.keys())
        self.num_threads = num_threads
        self.time_start = datetime.datetime.utcnow()
        self.workers = []

    def refresh_feed(self, feed_id):
        """Update feed, since it may have changed"""
        return Feed.objects.using('default').get(pk=feed_id)
        
    def process_feed_wrapper(self, feed_queue):
        delta = None
        current_process = multiprocessing.current_process()
        identity = "X"
        feed = None
        
        if current_process._identity:
            identity = current_process._identity[0]
            
        for feed_id in feed_queue:
            start_duration = time.time()
            feed_fetch_duration = None
            feed_process_duration = None
            page_duration = None
            feed_code = None
            ret_entries = None
            start_time = time.time()
            ret_feed = FEED_ERREXC
            try:
                feed = self.refresh_feed(feed_id)
                
                ffeed = FetchFeed(feed_id, self.options)
                # fetch method will get the html about feed_address
                # ret_feed stands for status, and fetched_feed stands for result.
                ret_feed, fetched_feed = ffeed.fetch()
                feed_fetch_duration = time.time() - start_duration
                
                if ((fetched_feed and ret_feed == FEED_OK) or self.options['force']):
                    pfeed = ProcessFeed(feed_id, fetched_feed, self.options)
                    ret_feed, ret_entries = pfeed.process()
                    feed = pfeed.feed
                    feed_process_duration = time.time() - start_duration
                    
                    if (ret_entries and ret_entries['new']) or self.options['force']:
                        start = time.time()
                        if not feed.known_good or not feed.fetched_once:
                            feed.known_good = True
                            feed.fetched_once = True
                            feed = feed.save()
            except urllib2.HTTPError, e:
                logging.debug('   ---> [%-30s] ~FRFeed throws HTTP error: ~SB%s' % (unicode(feed_id)[:30], e.fp.read()))
                feed.save_feed_history(e.code, e.msg, e.fp.read())
                fetched_feed = None
            except Feed.DoesNotExist, e:
                logging.debug('   ---> [%-30s] ~FRFeed is now gone...' % (unicode(feed_id)[:30]))
                continue
            except TimeoutError, e:
                logging.debug('   ---> [%-30s] ~FRFeed fetch timed out...' % (feed.title[:30]))
                feed.save_feed_history(505, 'Timeout', e)
                feed_code = 505
                fetched_feed = None
            except Exception, e:
                logging.debug('[%d] ! -------------------------' % (feed_id,))
                tb = traceback.format_exc()
                logging.error(tb)
                logging.debug('[%d] ! -------------------------' % (feed_id,))
                ret_feed = FEED_ERREXC 
                feed = Feed.get_by_id(getattr(feed, 'pk', feed_id))
                if not feed: continue
                feed.save_feed_history(500, "Error", tb)
                feed_code = 500
                fetched_feed = None

            if not feed_code:
                if ret_feed == FEED_OK:
                    feed_code = 200
                elif ret_feed == FEED_SAME:
                    feed_code = 304
                elif ret_feed == FEED_ERRHTTP:
                    feed_code = 400
                if ret_feed == FEED_ERREXC:
                    feed_code = 500
                elif ret_feed == FEED_ERRPARSE:
                    feed_code = 550
                elif ret_feed == FEED_ERRPARSE:
                    feed_code = 550
                
            if not feed: continue
            feed = self.refresh_feed(feed.pk)

            delta = time.time() - start_time
            
            feed.last_load_time = round(delta)
            feed.fetched_once = True
            try:
                feed = feed.save()
            except IntegrityError:
                logging.debug("   ---> [%-30s] ~FRIntegrityError on feed: %s" % (feed.title[:30], feed.feed_address,))
                
            done_msg = (u'%2s ---> [%-30s] ~FYProcessed in ~FM~SB%.4ss~FY~SN (~FB%s~FY) [%s]' % (
                identity, feed.feed_title[:30], delta,
                feed.pk, self.feed_trans[ret_feed],))
            logging.debug(done_msg)
            total_duration = time.time() - start_duration
            # MAnalyticsFetcher.add(feed_id=feed.pk, feed_fetch=feed_fetch_duration,
            #                       feed_process=feed_process_duration, 
            #                       page=page_duration, total=total_duration, feed_code=feed_code)
            
            self.feed_stats[ret_feed] += 1       
        
        if len(feed_queue) == 1:
            return feed
            
    def add_jobs(self, feeds_queue, feeds_count=1):
        """ adds a feed processing job to the pool
        """
        self.feeds_queue = feeds_queue
        self.feeds_count = feeds_count
            
    # if the single_threaded option is ture, run in this thread. 
    def run_jobs(self):
        if self.options['single_threaded']:
            return self.process_feed_wrapper(self.feeds_queue[0])
        else:
            for i in range(self.num_threads):
                feed_queue = self.feeds_queue[i]
                self.workers.append(multiprocessing.Process(target=self.process_feed_wrapper,
                                                            args=(feed_queue,)))
            for i in range(self.num_threads):
                self.workers[i].start()
