"""SOAP API client AlgoSec **FirewallAnalyzer**.


Clients require four arguments to be initiated (and total of five):

* AlgoSec server IP
* username
* password
* email
* *verify_ssl* (optional)

Examples:
    Once initiated, the client is used by calling any of its public functions::

        from algosec.api_clients.firewall_analyzer import FirewallAnalyzerAPIClient
        client = FirewallAnalyzerAPIClient(ip, username, password)
        query_result = client.run_traffic_simulation_query(
            source,
            dest,
            service
        )

    If the API call you were looking for is not yet implemented, you can send authenticated custom API call
    to the server using the client's ``session`` property.
    Please see specific API Client documentations to find out how.
"""
import logging
from collections import OrderedDict

from deprecated import deprecated

from algosec.constants import API_CALL_FAILED_RESPONSE, PERMISSION_ERROR_MSG, TSQ_NO_PERMISSION, \
    LOGIN_FAILED_IMPERSONATION_REASON, LOGIN_FAILED_IMPERSONATION_DETAILS, LOGIN_FAILED_IMPERSONATION_MSG
from algosec.api_clients.base import SoapAPIClient, APIClient
from algosec.helpers import report_soap_failure
from algosec.errors import (
    AlgoSecLoginError,
    AlgoSecAPIError,
    UnrecognizedAllowanceState,
    UnauthorizedUserException,
)
from algosec.models import DeviceAllowanceState

from zeep.exceptions import Fault

import re

logger = logging.getLogger(__name__)


class FirewallAnalyzerAPIClient(SoapAPIClient):
    """*FirewallAnalyzer* SOAP API client.

    Used by initiating and calling its public methods or by sending custom calls using the ``client`` property.
    Client implementation is strictly based on AlgoSec's official API guide.

    Example:

        Using the public methods to send an API call::

            from algosec.api_clients.firewall_analyzer import FirewallAnalyzerAPIClient
            client = FirewallAnalyzerAPIClient(ip, username, password)
            query_result = client.run_traffic_simulation_query(source, dest, service)

    Args:
        server_ip (str): IP address of the AlgoSec server.
        user (str): Username used to log in to AlgoSec.
        password (str): The user's password, similar to the one used to log in to the UI.
        verify_ssl (bool): Turn on/off the connection's SSL certificate verification. Defaults to True.

    """

    @property
    def _wsdl_url_path(self):
        return "https://{}/AFA/php/ws.php?wsdl".format(self.server_ip)

    @property
    def _soap_service_location(self):  # pragma: no cover
        return "https://{}/AFA/php/ws.php".format(self.server_ip)

    @property
    def afa_session_id_getter(self):
        """
        Getter to afa session id. Initiates Client if necessary.
        In case Client Initiation fails for some reason, afa session id might be none.
        :return: afa session id.
        """
        # if client isn't initiated, property calls _initiate_client method.
        try:
            if self.client is None:
                # shouldn't reach here.
                logger.warning("Client is not initiated, afa session id might have None Value.")
        except UnauthorizedUserException as e:
            logger.exception("Login failed since user doesn't exist, afa session id have None Value.")
            return None
        return self._session_id

    def _soap_service(self, client):
        """
        Initialize service with correct location if necessary and return it.

        Args:
            client: initialized zeep client.

        Returns:
            a zeep ProxyService object.
        """
        if self._service is None:
            binding = "{}".format(client.service._binding.name.text)
            self._service = client.create_service(binding, self._soap_service_location)
        return self._service

    def _initiate_client(self):
        """Return a connected zeep client and save the new session id to ``self._session_id``

        Raises:
            AlgoSecLoginError: If login using the username/password failed.

        Returns:
            zeep.Client
        """


        client = self._get_soap_client(
            self._wsdl_url_path, location=self._soap_service_location
        )
        with report_soap_failure(AlgoSecLoginError):
            try:
                self._session_id = self._soap_service(client).connect(
                    UserName=self.user, Password=self.password, Domain="",
                    ImpersonateUser=self.user_email
                )
                APIClient._impersonation_success = True
                logger.debug("Impersonation succeeded for user-email: {}".format(self.user_email))
            except Fault as e:
                if e.message is not None and LOGIN_FAILED_IMPERSONATION_REASON in e.message:
                    if self.algobot_login_user is not None and self.algobot_login_password is not None:
                        logger.debug("Impersonation failed, trying to connect to AlgoBot Login User.")
                        try:
                            self._session_id = self._soap_service(client).connect(
                                UserName=self.algobot_login_user, Password=self.algobot_login_password, Domain=""
                            )
                            logger.debug("Login succeeded for Algobot's default user.")
                        except Fault as e:
                            # impersonation failed and AlgoBot login user isn't defined in Algosec Servers.
                            if e.message is not None and LOGIN_FAILED_IMPERSONATION_REASON in e.message:
                                logger.debug("Impersonation failed, {} in Algosec Servers."
                                             .format(LOGIN_FAILED_IMPERSONATION_DETAILS.format(self.user_email)))
                                raise UnauthorizedUserException(LOGIN_FAILED_IMPERSONATION_MSG,
                                                                "{} in Algosec Servers."
                                                                .format(LOGIN_FAILED_IMPERSONATION_DETAILS
                                                                        .format(self.user_email)))
                            raise e
                    else:
                        # impersonation failed and AlgoBot login user isn't defined in configuration file.
                        logger.debug("Impersonation failed, {} in AlgoBot configuration file."
                                     .format(LOGIN_FAILED_IMPERSONATION_DETAILS.format(self.user_email)))
                        raise UnauthorizedUserException(LOGIN_FAILED_IMPERSONATION_MSG,
                                                        "{} in AlgoBot configuration file."
                                                        .format(LOGIN_FAILED_IMPERSONATION_DETAILS
                                                                .format(self.user_email)))
                else:
                    raise e

        return client

    @staticmethod
    def _prepare_simulation_query_results(devices):
        """Return traffic simulation query results aggregated by device allowance state"""
        query_results = OrderedDict(
            [
                (DeviceAllowanceState.BLOCKED, []),
                (DeviceAllowanceState.PARTIALLY_BLOCKED, []),
                (DeviceAllowanceState.ALLOWED, []),
            ]
        )
        # Group the devices by groups according to their device result
        for device in devices:
            try:
                allowance_state = DeviceAllowanceState.from_string(device.IsAllowed)
                query_results[allowance_state].append(device)
            except UnrecognizedAllowanceState:
                logger.warning(
                    "Unknown device state found. Device: {}, state: {}".format(
                        device, device.IsAllowed
                    )
                )
        return query_results

    @staticmethod
    def _calc_aggregated_query_result(query_results):
        """Return aggregated calculated traffic query result.

        Since we had the "QueryResult" missing from the API before AlgoSec version 2017.02 we check here if it is
        part of the result. If not, we try and calculate the traffic query result based on the results we got
        for the various devices under the query.

        Returns:
            algosec.models.DeviceAllowanceState: Aggregated traffic simulation result.
        """
        # Understanding the value of the total result, is the traffic blocked or allowed or partially blocked?
        if query_results[DeviceAllowanceState.PARTIALLY_BLOCKED]:
            return DeviceAllowanceState.PARTIALLY_BLOCKED
        elif query_results[DeviceAllowanceState.BLOCKED]:
            if query_results[DeviceAllowanceState.ALLOWED]:
                # Result contain both blocked and allowed, thus it is partial
                return DeviceAllowanceState.PARTIALLY_BLOCKED
            # Only blocked
            return DeviceAllowanceState.BLOCKED
        # No partial or blocked results, so it is assumed to be allowed
        return DeviceAllowanceState.ALLOWED

    @classmethod
    def _get_summarized_query_result(cls, query_response, query_results):
        """
        Return final simulation query result.

        The final result is fetched directly from the soap response object if it is available.
        Otherwise, it is manually calculated from the simulation query results per device.

        This function is needed as the final "QueryResult" was missing from the API before AlgoSec version 2017.02.
        Therefore we first check here if it is part of the result. If not, we try and calculate the traffic
        query result based on the results we got for the various devices for the query.

        Args:
            query_response: Soap response for the simulation query soap call.
            query_results: Results for the simulation query soap call per network devices grouped by their allowance
             state.

        Returns:
             algosec.models.DeviceAllowanceState: The simulation query final result

        """
        if getattr(query_response, "QueryResult", None):
            aggregated_result = DeviceAllowanceState.from_string(
                query_response.QueryResult
            )
        else:
            aggregated_result = cls._calc_aggregated_query_result(query_results)
        return aggregated_result

    def _execute_traffic_simulation_query(
        self, source, destination, service, target=None, application=None
    ):
        with report_soap_failure(AlgoSecAPIError):
            params = dict(
                QueryInput={
                    "Source": source,
                    "Destination": destination,
                    "Service": service,
                }
            )
            if application:
                params["QueryInput"]["Application"] = application
            if target is not None:
                params["QueryTarget"] = target

            logger.debug(self._api_info_string.format(
                "Traffic Simulation Query",
                self._wsdl_url_path + " op_name: query",
                params
            ))
            try:
                simulation_query_response = self._soap_service(self.client).query(
                    SessionID=self._session_id, **params
                ).QueryResult
            except Fault as err:
                err_code = ''
                err_code_match = re.search('\d+', err.args[0])
                if err_code_match:
                    err_code = err_code_match.group(0)
                # if there are no permissions (505), raise a new type of exception - UnauthorizedUserException.
                if err_code == '505':
                    raise UnauthorizedUserException(PERMISSION_ERROR_MSG, TSQ_NO_PERMISSION.format(
                        PERMISSION_ERROR_MSG, self.user_email, err_code
                    ))
                raise

        logger.debug("response: {}".format(simulation_query_response or API_CALL_FAILED_RESPONSE))
        query_url = getattr(simulation_query_response[0], "QueryHTMLPath", None)
        if (
                simulation_query_response is None
                or not simulation_query_response[0].QueryItem
        ):
            devices = []
        else:
            devices = simulation_query_response[0].QueryItem.Device
            if type(devices) is not list:
                # In case there is only one object in the result, we listify the object
                devices = [devices]
        # Making a dict from the result type to a list of devices. Keep it always ordered by the result type
        query_results = self._prepare_simulation_query_results(devices)
        return query_results, query_url, simulation_query_response

    @deprecated(
        version="1.2.0",
        reason="This function will be removed soon. Please use `execute_traffic_simulation_query` instead.",
    )
    def run_traffic_simulation_query(self, source, destination, service):
        """
        Run a traffic simulation query.

        Args:
            source (str): Source of the simulated traffic. (e.g. IPs, subnet or an object name)
            destination (str): Destination of the simulated traffic. (e.g. IPs, subnet or an object name)
            service (str): Service of the simulated traffic (e.g: tcp/200, http)

        Raises:
            :class:`~algosec.errors.AlgoSecAPIError`: If any error occurred while executing the traffic
                simulation query.

        Returns:
            algosec.models.DeviceAllowanceState: Traffic simulation query result.
        """
        query_results, query_url, simulation_query_response = self._execute_traffic_simulation_query(
            source, destination, service
        )
        return self._get_summarized_query_result(
            simulation_query_response[0], query_results
        )

    def execute_traffic_simulation_query(
        self, source, destination, service, target=None, application=None
    ):
        """
        Return results and browser URL for a traffic simulation query.

        Args:
            source (str): Source of the simulated traffic. (e.g. IPs, subnet or an object name)
            destination (str): Destination of the simulated traffic. (e.g. IPs, subnet or an object name)
            service (str): Service of the simulated traffic (e.g: tcp/200, http)
            target (str): Name of a device or a group the query should run on.
                With the default None value, the query will run on the entire network
                and all permitted devices for the user.
            application (str): Name of the network application to include in the query.

        Raises:
            :class:`~algosec.errors.AlgoSecAPIError`: If any error occurred while executing the traffic
                simulation query.

        Returns:
            dict: A dict mapping the results to their values. For example:

            {
                'result': DeviceAllowanceState.ALLOWED,
                'query_url': 'https://local.algosec.com/fa/query/results/#/work/ALL_FIREWALLS_query-1543622562206/'
            }
        """
        query_results, query_url, simulation_query_response = self._execute_traffic_simulation_query(
            source, destination, service, target=target, application=application
        )
        return {
            "result": self._get_summarized_query_result(
                simulation_query_response[0], query_results
            ),
            "raw_response": simulation_query_response,
            "query_url": query_url,
        }
