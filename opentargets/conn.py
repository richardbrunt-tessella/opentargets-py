"""
This module abstracts the connection to the Open Targets REST API to simplify its usage.
Can be used directly but requires some knowledge of the API.
"""
import gzip
import json
import logging
from collections import namedtuple
from itertools import islice
from json import JSONEncoder
import collections

import addict
import requests
from cachecontrol import CacheControl
from future.utils import implements_iterator
import yaml
from requests.adapters import HTTPAdapter
from urllib3 import Retry
from opentargets.version import __version__, __api_major_version__

try:
    import pandas
    pandas_available = True
except ImportError:
    pandas_available = False

try:
    import xlwt
    xlwt_available = True
except ImportError:
    xlwt_available = False

try:
    from tqdm import tqdm
    tqdm_available = True
except ImportError:
    tqdm_available = False

API_MAJOR_VERSION = __api_major_version__

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

def flatten(d, parent_key='', separator='.'):
    """
    Takes a nested dictionary as input and generate a flat one with keys separated by the separator

    Args:
        d (dict): dictionary
        parent_key (str): a prefix for all flattened keys
        separator (str): separator between nested keys

    Returns:
        dict: a flattened dictionary
    """
    flat_fields = []
    for k, v in d.items():
        flat_key = parent_key + separator + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            flat_fields.extend(flatten(v, flat_key, separator=separator).items())
        else:
            flat_fields.append((flat_key, v))
    return dict(flat_fields)

def compress_list_values(d, sep='|'):
    """
    Args:
        d (dict): dictionary
        sep (str): separator char used to join list element

    Returns:
        dict: dictionary with compressed lists
    """
    for k, v in d.items():
        if not isinstance(v, (str, int, float)):
            if isinstance(v, collections.Sequence):
                safe_values = []
                for i in v:
                    if isinstance(i, (str, int, float)):
                        safe_values.append(str(i))
                    else:
                        safe_values.append(json.dumps(i))
                d[k]=sep.join(safe_values)
    return d



class HTTPMethods(object):
    GET='get'
    POST='post'


class Response(object):
    """
    Handler for responses coming from the api
    """

    def __init__(self, response):
        """

        Args:
            response: a response coming from a requests call
            content_type (str): content type of the response
        """
        self._logger = logging.getLogger(__name__)
        try:
            # TODO parse from json just if content type allows it
            parsed_response = response.json()
            if isinstance(parsed_response, dict):
                if 'data' in parsed_response:
                    self.data = parsed_response['data']
                    del parsed_response['data']
                else:
                    self.data = [parsed_response]
                if 'from' in parsed_response:
                    parsed_response['from_'] = parsed_response['from']
                    del parsed_response['from']
                if 'next' in parsed_response:
                    parsed_response['next_'] = parsed_response['next']
                    del parsed_response['next']
                self.info = addict.Dict(parsed_response)

            else:
                # TODO because content type wasnt checked a string
                # is converted to a float without notice
                self.data = parsed_response
                self.info = {}

        except ValueError as e:
            self.data = response.text
            self.info = {}

        self._headers = response.headers

    def __str__(self):
        if self.data:
            data = str(self.data)
            if len(data)>100:
                return data[:50]+' ...,'+data[-50:]
        else:
            return ''

    def __len__(self):
        try:
            return self.info.total
        except:
            return len(self.data)


class Connection(object):
    """
    Handler for connection and calls to the Open Targets Validation Platform REST API
    """
    def __init__(self,
                 host='https://api.opentargets.io',
                 port=443,
                 api_version='v3',
                 verify = True,
                 proxies = {}
                 ):
        """
        Args:
            host (str): host serving the API
            port (int): port to use for connection to the API
            api_version (str): api version to point to, default to 'latest'
            verify (bool): sets SSL verification for Request session, accepts True, False or a path to a certificate
        """
        self._logger = logging.getLogger(__name__)
        self.host = host
        self.port = str(port)
        self.api_version = api_version
        session= requests.Session()
        session.verify = verify
        session.proxies = proxies
        retry_policies = Retry(total=10,
                               read=10,
                               connect=10,
                               backoff_factor=.5,
                               status_forcelist=(500, 502, 504),)
        http_retry = HTTPAdapter(max_retries=retry_policies)
        session.mount(host, http_retry)
        self.session = CacheControl(session)
        self._get_remote_api_specs()



    def _build_url(self, endpoint):
        url = '{}:{}/{}{}'.format(self.host,
                                       self.port,
                                       self.api_version,
                                       endpoint,)
        return url

    @staticmethod
    def _auto_detect_post(params):
        """
        Determine if a post request should be made instead of a get depending on the size of the parameters
        in the request.

        Args:
            params (dict): params to pass in the request

        Returns:
            Boolean: True if post is needed
        """
        if params:
            for k,v in params.items():
                if isinstance(v, (list, tuple)):
                    if len(v)>3:
                        return True
        return False

    def get(self, endpoint, params=None):
        """
        makes a GET request
        Args:
            endpoint (str): REST API endpoint to call
            params (dict): request payload

        Returns:
            Response: request response
        """
        if self._auto_detect_post(params):
            self._logger.debug('switching to POST due to big size of params')
            return self.post(endpoint, data=params)
        return Response(self._make_request(endpoint,
                              params=params,
                              method='GET'))

    def post(self, endpoint, data=None):
        """
        makes a POST request
        Args:
            endpoint (str): REST API endpoint to call
            data (dict): request payload

        Returns:
            Response: request response
        """
        return Response(self._make_request(endpoint,
                               data=data,
                               method='POST'))

    def _make_request(self,
                      endpoint,
                      params = None,
                      data = None,
                      method = HTTPMethods.GET,
                      headers = {},
                      rate_limit_fail = False,
                      **kwargs):
        """
        Makes a request to the REST API
        Args:
            endpoint (str): endpoint of the REST API
            params (dict): payload for GET request
            data (dict): payload for POST request
            method (HTTPMethods): request method, either HTTPMethods.GET or HTTPMethods.POST. Defaults to HTTPMethods.GET
            headers (dict): HTTP headers for the request
            rate_limit_fail (bool): If True raise exception when usage limit is exceeded. If False wait and
                retry the request. Defaults to False.
        Keyword Args:
            **kwargs: forwarded to requests

        Returns:
            a response from requests
        """


        'order params to allow efficient caching'
        if params:
            if isinstance(params, dict):
                params = sorted(params.items())
            else:
                params = sorted(params)

        headers['User-agent'] = 'Open Targets Python Client/%s' % str(__version__)
        response = self.session.request(method,
                                    self._build_url(endpoint),
                                    params=params,
                                    json=data,
                                    headers=headers,
                                    **kwargs)

        response.raise_for_status()
        return response

    def _get_remote_api_specs(self):
        """
        Fetch and parse REST API documentation
        """
        r= self.session.get(self.host+':'+self.port+'/v%s/platform/swagger'%API_MAJOR_VERSION)
        r.raise_for_status()
        self.swagger_yaml = r.text
        self.api_specs = yaml.load(self.swagger_yaml)
        self.endpoint_validation_data={}
        for p, data in self.api_specs['paths'].items():
            p=p.split('{')[0]
            if p[-1]== '/':
                p=p[:-1]
            self.endpoint_validation_data[p] = {}
            self.endpoint_validation_data['/platform' + p] = {}
            for method, method_data in data.items():
                if 'parameters' in method_data:
                    params = {}
                    for par in method_data['parameters']:
                        par_type = par.get('type', 'string')
                        params[par['name']]=par_type
                    self.endpoint_validation_data[p][method] = params
                    self.endpoint_validation_data['/platform' + p][method] = params

        remote_version = self.get('/platform/public/utils/version').data
        # TODO because content type wasnt checked proerly a float
        # was returned instead a proper version string
        if not str(remote_version).startswith(API_MAJOR_VERSION):
            self._logger.warning('The remote server is running the API with version {}, but the client expected this major version {}. They may not be compatible.'.format(remote_version, API_MAJOR_VERSION))

    def validate_parameter(self, endpoint, filter_type, value, method=HTTPMethods.GET):
        """
        Validate payload to send to the REST API based on info fetched from the API documentation

        Args:
            endpoint (str): endpoint of the REST API
            filter_type (str): the parameter sent for the request
            value: the value sent for the request
            method (HTTPMethods): request method, either HTTPMethods.GET or HTTPMethods.POST. Defaults to HTTPMethods.GET
        Raises
            AttributeError: if validation is not passed

        """

        endpoint_data = self.endpoint_validation_data[endpoint][method]
        if filter_type in endpoint_data:
            if endpoint_data[filter_type] == 'string' and isinstance(value, str):
                return
            elif endpoint_data[filter_type] == 'boolean' and isinstance(value, bool):
                return
            elif endpoint_data[filter_type] == 'number' and isinstance(value, (int, float)):
                return

        raise AttributeError('{}={} is not a valid parameter for endpoint {}'.format(filter_type, value, endpoint))

    def api_endpoint_docs(self, endpoint):
        """
        Returns the documentation available for a given REST API endpoint

        Args:
            endpoint (str): endpoint of the REST API

        Returns:
            dict: documentation for the endpoint parsed from YAML docs
        """
        return self.api_specs['paths'][endpoint]

    def get_api_endpoints(self):
        """
        Get a list of available endpoints

        Returns:
            list: available endpoints
        """
        return self.api_specs['paths'].keys()

    def close(self):
        """
        Close connection to the REST API
        """
        self.session.close()

    def ping(self):
        """
        Pings the API as a live check
        Returns:
            bool: True if pinging the raw response as a ``str`` if the API has a non standard name
        """
        response = self.get('/platform/public/utils/ping')
        if response.data=='pong':
            return True
        elif response.data:
            return response.data
        return False

@implements_iterator
class IterableResult(object):
    '''
    Proxy over the Connection class that allows to iterate over all the items returned from a quer.
    It will automatically handle making multiple calls for pagination if needed.
    '''
    def __init__(self, conn, method = HTTPMethods.GET):
        """
        Requires a Connection
        Args:
            conn (Connection): a Connection instance
            method (HTTPMethods): HTTP method to use for the calls
        """
        self.conn = conn
        self.method = method
        self._search_after_last = None

    def __call__(self, *args, **kwargs):
        """
        Allows to set parameters for calls to the REST API
        Args:
            *args: stored internally
        Keyword Args:
            **kwargs: stored internally

        Returns:
            IterableResult: returns itself
        """
        self._args = args
        self._kwargs = kwargs
        response = self._make_call()
        self.info = response.info
        self._data = response.data
        if 'next_' in response.info:
            self._search_after_last = response.info.next_
        self.current = 0
        try:
            self.total = int(self.info.total)
            if 'size' in self.info and  'size' not in self._kwargs:
                self._kwargs['size']=1000
        except:
            self.total = len(self._data)
        finally:
            return self

    def filter(self, **kwargs):
        """
        Applies a set of filters to the current query
        Keyword Args
            **kwargs: passed to the REST API
        Returns:
            IterableResult: an IterableResult with applied filters
        """
        if kwargs:
            for filter_type, filter_value in kwargs.items():
                self._validate_filter(filter_type, filter_value)
                self._kwargs[filter_type] = filter_value
            self.__call__(*self._args, **self._kwargs)
        return self


    def _make_call(self):
        """
        makes calls to the REST API
        Returns:
            Response: response for a call
        Raises:
            AttributeError: if HTTP method is not supported
        """
        if self.method == HTTPMethods.GET:
            return self.conn.get(*(self._args), params=self._kwargs)
        elif self.method == HTTPMethods.POST:
            return self.conn.post(*self._args, data=self._kwargs)
        else:
            raise AttributeError("HTTP method {} is not supported".format(self.method))

    def __iter__(self):
        return self

    def __next__(self):
        if self.current < self.total:
            if not self._data:
                if self._search_after_last:
                    self._kwargs['from'] = 0
                    self._kwargs['next'] = self._search_after_last
                else:
                    self._kwargs['from'] = self.current
                self._kwargs['no_cache']='true'
                self._kwargs['size'] = 1000
                call_output = self._make_call()
                if not call_output.data:
                    raise StopIteration
                if 'next_' in call_output.info:
                    self._search_after_last = call_output.info.next_
                self._data = call_output.data
            d = self._data.pop(0)
            self.current+=1
            return d
        else:
            raise StopIteration()

    def __len__(self):
        try:
            return self.total
        except:
            return 0

    def __bool__(self):
        return self.__len__() >0

    def __nonzero__(self):
        return self.__bool__()

    def __str__(self):
        try:
            return_str = '{} Results found'.format(self.total)
            if self._kwargs:
                return_str+=' | parameters: {}'.format(self._kwargs)
            return return_str
        except:
            data = str(self._data)
            return data[:100] + (data[100:] and '...')

    def __repr__(self):
        return self.__str__()

    def __getitem__(self, x):
        if type(x) is slice:
            return list(islice(self, x.start, x.stop, x.step))
        else:
            return next(islice(self, x, None), None)

    def _validate_filter(self,filter_type, value):
        """
        validate the provided filter versus the REST API documentation
        Args:
            filter_type (str): filter for the REST API call
            value: the value passed

       Raises
            AttributeError: if validation is not passed

        """
        self.conn.validate_parameter(self._args[0], filter_type, value)

    def to_json(self,iterable=True, **kwargs):
        """

        Args:
            iterable: If True will yield a json string for each result and convert them dinamically as they are
                fetched from the api. If False gets all the results and returns a singl json string.
        Keyword Args:
            **kwargs: forwarded to json.dumps

        Returns:
            an iterator of json strings or a single json string
        """
        if iterable:
            return (json.dumps(i) for i in self)
        return IterableResultSimpleJSONEncoder(**kwargs).encode(self)


    def to_dataframe(self, compress_lists = False,**kwargs):
        """
        Create a Pandas dataframe from a flattened version of the response.

        Args:
            compress_lists: if a value is a list, serialise it to a string with '|' as separator
        Keyword Args:
            **kwargs: forwarded to pandas.DataFrame.from_dict

        Returns:
            pandas.DataFrame: A DataFrame with all the data coming from the query in the REST API
        Notes:
            Requires Pandas to be installed.
        Raises:
            ImportError: if Pandas is not available

        """
        if pandas_available:
            data = [flatten(i) for i in self]
            if compress_lists:
                data = [compress_list_values(i) for i in data]

            return pandas.DataFrame.from_dict(data,  **kwargs)
        else:
            raise ImportError('Pandas library is not installed but is required to create a dataframe')

    def to_csv(self, **kwargs):
        """
        Create a csv file from a flattened version of the response.

        Keyword Args:
            **kwargs: forwarded to pandas.DataFrame.to_csv
        Returns:
            output of pandas.DataFrame.to_csv
        Notes:
            Requires Pandas to be installed.
        Raises:
            ImportError: if Pandas is not available

        """
        return self.to_dataframe(compress_lists=True).to_csv(**kwargs)


    def to_excel(self, excel_writer, **kwargs):
        """
        Create a excel (xls) file from a flattened version of the response.

        Keyword Args:
            **kwargs: forwarded to pandas.DataFrame.to_excel
        Returns:
            output of pandas.DataFrame.to_excel
        Notes:
            Requires Pandas and xlwt to be installed.
        Raises:
            ImportError: if Pandas or xlwt are not available

        """
        if xlwt_available:
            self.to_dataframe(compress_lists=True).to_excel(excel_writer, **kwargs)
        else:
            raise ImportError('xlwt library is not installed but is required to create an excel file')

    def to_object(self):
        """
        Converts dictionary in the data to an addict object. Useful for interactive data exploration on IPython
            and similar tools
        Returns:
            iterator: an iterator of addict.Dict
        """
        return (addict.Dict(i) for i in self)

    def to_file(self, filename, compress=True, progress_bar = False):
        if compress:
            fh = gzip.open(filename, 'wb')
        else:
            fh = open(filename, 'wb')
        if tqdm_available and progress_bar:
            progress = tqdm(desc='Saving entries to file %s'%filename,
                       total=len(self),
                       unit_scale=True)
        for datapoint in self:
            line = json.dumps(datapoint)+'\n'
            fh.write(line.encode('utf-8'))
            if tqdm_available and progress_bar:
                progress.update()
        fh.close()


class IterableResultSimpleJSONEncoder(JSONEncoder):
    def default(self, o):
        '''extends JsonEncoder to support IterableResult'''
        if isinstance(o, IterableResult):
            return list(o)
