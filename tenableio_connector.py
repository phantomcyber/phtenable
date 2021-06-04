#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2021 Splunk Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#--

# Python 3 Compatibility imports
from __future__ import print_function, unicode_literals

# Phantom App imports
import phantom.app as phantom
from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

# Usage of the consts file is recommended
# from tenableio_consts import *
import requests
import json
from bs4 import BeautifulSoup

import ast
import dateutil.parser
import traceback

from datetime import datetime
from tenable.io import TenableIO

from typing import List, Any, Callable, Union


class RetVal(tuple):

    def __new__(cls, val1, val2=None):
        return tuple.__new__(RetVal, (val1, val2))


class TenableioConnector(BaseConnector):

    def __init__(self):

        # Call the BaseConnectors init first
        super(TenableioConnector, self).__init__()

        self._state = None

        # Variable to hold a base_url in case the app makes REST calls
        # Do note that the app json defines the asset config, so please
        # modify this as you deem fit.
        self._base_url = None

    def _process_empty_response(self, response, action_result):
        if response.status_code == 200:
            return RetVal(phantom.APP_SUCCESS, {})

        return RetVal(
            action_result.set_status(
                phantom.APP_ERROR, "Empty response and no information in the header"
            ), None
        )

    def _process_html_response(self, response, action_result):
        # An html response, treat it like an error
        status_code = response.status_code

        try:
            soup = BeautifulSoup(response.text, "html.parser")
            error_text = soup.text
            split_lines = error_text.split("\n")
            split_lines = [x.strip() for x in split_lines if x.strip()]
            error_text = "\n".join(split_lines)
        except Exception:
            error_text = "Cannot parse error details"

        message = "Status Code: {0}. Data from server:\n{1}\n".format(status_code, error_text)

        message = message.replace(u"{", "{{").replace(u"}", "}}")
        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _process_json_response(self, r, action_result):
        # Try a json parse
        try:
            resp_json = r.json()
        except Exception as e:
            return RetVal(
                action_result.set_status(
                    phantom.APP_ERROR, "Unable to parse JSON response. Error: {0}".format(str(e))
                ), None
            )

        # Please specify the status codes here
        if 200 <= r.status_code < 399:
            return RetVal(phantom.APP_SUCCESS, resp_json)

        # You should process the error returned in the json
        message = "Error from server. Status Code: {0} Data from server: {1}".format(
            r.status_code,
            r.text.replace(u"{", "{{").replace(u"}", "}}")
        )

        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _process_response(self, r, action_result):
        # store the r_text in debug data, it will get dumped in the logs if the action fails
        if hasattr(action_result, "add_debug_data"):
            action_result.add_debug_data({"r_status_code": r.status_code})
            action_result.add_debug_data({"r_text": r.text})
            action_result.add_debug_data({"r_headers": r.headers})

        # Process each "Content-Type" of response separately

        # Process a json response
        if "json" in r.headers.get("Content-Type", ""):
            return self._process_json_response(r, action_result)

        # Process an HTML response, Do this no matter what the api talks.
        # There is a high chance of a PROXY in between phantom and the rest of
        # world, in case of errors, PROXY"s return HTML, this function parses
        # the error and adds it to the action_result.
        if "html" in r.headers.get("Content-Type", ""):
            return self._process_html_response(r, action_result)

        # it"s not content-type that is to be parsed, handle an empty response
        if not r.text:
            return self._process_empty_response(r, action_result)

        # everything else is actually an error at this point
        message = "Can't process response from server. Status Code: {0} Data from server: {1}".format(
            r.status_code,
            r.text.replace("{", "{{").replace("}", "}}")
        )

        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _make_rest_call(self, endpoint, action_result, method="get", **kwargs):
        # **kwargs can be any additional parameters that requests.request accepts

        config = self.get_config()

        resp_json = None

        try:
            request_func = getattr(requests, method)
        except AttributeError:
            return RetVal(
                action_result.set_status(phantom.APP_ERROR, "Invalid method: {0}".format(method)),
                resp_json
            )

        # Create a URL to connect to
        url = self._base_url + endpoint

        try:
            r = request_func(
                url,
                # auth=(username, password),  # basic authentication
                verify=config.get("verify_server_cert", False),
                **kwargs
            )
        except Exception as e:
            return RetVal(
                action_result.set_status(
                    phantom.APP_ERROR, "Error Connecting to server. Details: {0}".format(str(e))
                ), resp_json
            )

        return self._process_response(r, action_result)

    def _parse_list_field(self, input_str: str, item_type: Callable = str) -> List[Any]:
        """
        Parses a string input into a list.
        The input string can be a JSON formatted list or comma separated list.
        Output with be the parsed input string as a list.

        Args:
            input_str (str):
                input string to parse
            item_type (Callable):
                a function to convert the type of each item of the list.
                an exception is thrown if converstion fails.
                (default: str)

        Returns:
            List representation of parsed input. If input is the empty string, empty list is returned.
        """
        if not input_str:
            return []

        try:
            parsed_input = json.loads(input_str)
        except json.JSONDecodeError:
            parsed_input = input_str.split(",")

        try:
            parsed_input = [item_type(item.strip()) for item in parsed_input]
        except Exception:
            raise TypeError(
                "Invalid item value for string to list conversion. Expected item type {}, got {}".format(
                    str(item_type),
                    input_str
                )
            )
        return parsed_input

    def _parse_datetime_field(self, input_time: Union[str, int]) -> datetime:
        """
        Parses the input into a datetime object. Input can be a timestamp or iso formatted string

        Args:
            input_time (str, int):
                either a timestamp (epoch) integer, or an iso formatted datetime string

        Returns:
            Parsed datetime object
        """
        parsed_input = None
        try:
            parsed_input = datetime.fromtimestamp(int(input_time))
        except Exception:
            try:
                parsed_input = dateutil.parser.isoparse(input_time)
            except Exception:
                raise ValueError("Cannot parse datetime field: {}".format(input_time))
        return parsed_input

    def _handle_test_connectivity(self, param):
        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # NOTE: test connectivity does _NOT_ take any parameters
        # i.e. the param dictionary passed to this handler will be empty.
        # Also typically it does not add any data into an action_result either.
        # The status and progress messages are more important.

        self.save_progress("Testing connection to Tenable.io ...")

        try:
            self._tio.scans.list()
            action_result.set_status(phantom.APP_SUCCESS)
            self.save_progress("Test Connectivity Passed")
        except Exception as e:
            action_result.set_status(phantom.APP_ERROR, str(e))
            self.save_progress("Test Connectivity Failed.")
            self.save_progress(traceback.format_exc())

        return action_result.get_status()

    def _handle_assign_tags(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        action = param["action"]
        assets = param["assets"]
        tags = param["tags"]

        # Optional values should use the .get() function
        # optional_parameter = param.get("optional_parameter", "default_value")

        parsed_assets = self._parse_list_field(assets, item_type=str)
        parsed_tags = self._parse_list_field(tags, item_type=str)

        try:
            response = self._tio.assets.assign_tags(action, parsed_assets, parsed_tags)
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(response)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["assets_updated"] = len(parsed_assets)

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_tags(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        # required_parameter = param["required_parameter"]

        # Optional values should use the .get() function
        # optional_parameter = param.get("optional_parameter", "default_value")

        try:
            tags = self._tio.tags.list()
            data = [tag for tag in tags]
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["tag_count"] = len(data)

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_assets(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        # required_parameter = param["required_parameter"]

        # Optional values should use the .get() function
        created_at = param.get("created_at", "")
        updated_at = param.get("updated_at", "")

        try:
            parsed_created_at = int(self._parse_datetime_field(created_at).timestamp()) if created_at else None
            parsed_updated_at = int(self._parse_datetime_field(updated_at).timestamp()) if updated_at else None

            self.save_progress("Starting asset export.")
            assets = self._tio.exports.assets(
                created_at=parsed_created_at,
                updated_at=parsed_updated_at
            )
            data = [asset for asset in assets]
            self.save_progress("Asset export finished.")
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["asset_count"] = len(data)

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_agents(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        # required_parameter = param["required_parameter"]

        # Optional values should use the .get() function
        filters = param.get("filters", "")

        # We have to convert a string to a tuple for filters. ast.literal_eval provides a safe way to convert
        # this string to a python list of tuples without using eval. See python documentation:
        # https://docs.python.org/3/library/ast.html#ast.literal_eval
        parsed_filters = ast.literal_eval(filters) if filters else []

        try:
            agents = self._tio.agents.list(*parsed_filters)
            data = [agent for agent in agents]
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["agent_count"] = len(data)

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_scan_results(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        # required_parameter = param["required_parameter"]
        scan_id = param["scan_id"]

        # Optional values should use the .get() function
        # optional_parameter = param.get("optional_parameter", "default_value")

        try:
            data = self._tio.scans.results(scan_id)
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["scan_name"] = data["info"]["name"]
        summary["host_count"] = data["info"]["hostcount"]

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_scans(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        # required_parameter = param["required_parameter"]

        # Optional values should use the .get() function
        # optional_parameter = param.get("optional_parameter", "default_value")

        try:
            scans = self._tio.scans.list()
            data = [scan for scan in scans]
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["scan_count"] = len(data)

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_vulnerabilities(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the "param" dictionary

        # Required values can be accessed directly
        # required_parameter = param["required_parameter"]

        # Optional values should use the .get() function
        cidr_range = param.get("cidr_range", "")
        first_found = param.get("first_found", "")
        last_fixed = param.get("last_fixed", "")
        last_found = param.get("last_found", "")
        plugin_family = param.get("plugin_family", "")
        plugin_ids = param.get("plugin_ids", "")
        severity = param.get("severity", "")
        state = param.get("state", "")
        tags = param.get("tags", "")

        try:
            # Format parameters
            parsed_cidr_range = cidr_range or None

            parsed_first_found = int(self._parse_datetime_field(first_found).timestamp()) if first_found else None
            parsed_last_fixed = int(self._parse_datetime_field(last_fixed).timestamp()) if last_fixed else None
            parsed_last_found = int(self._parse_datetime_field(last_found).timestamp()) if last_found else None

            parsed_plugin_ids = self._parse_list_field(plugin_ids, item_type=int)
            parsed_plugin_family = self._parse_list_field(plugin_family, item_type=str)
            parsed_severity = self._parse_list_field(severity, item_type=str)
            parsed_state = self._parse_list_field(state, item_type=str)

            if tags:
                try:
                    parsed_tags = [(k, v) for k, v in json.loads(tags).iteritems()]
                except Exception:
                    raise ValueError("tags parameter must be a dict of key value pairs, got '{}' instead".format(tags))
            else:
                parsed_tags = []

            self.save_progress("Starting vulnerability export.")
            vulns = self._tio.exports.vulns(
                cidr_range=parsed_cidr_range,
                first_found=parsed_first_found,
                last_fixed=parsed_last_fixed,
                last_found=parsed_last_found,
                plugin_family=parsed_plugin_family,
                plugin_ids=parsed_plugin_ids,
                severity=parsed_severity,
                state=parsed_state,
                tags=parsed_tags
            )
            data = [vuln for vuln in vulns]
            self.save_progress("Vulnerability export finished successfully.")
        except Exception as e:
            self.save_progress(traceback.format_exc())
            return action_result.set_status(phantom.APP_ERROR, str(e))

        # Add the response into the data section
        action_result.add_data(data)

        # Add a dictionary that is made up of the most important values from data into the summary
        summary = action_result.update_summary({})
        summary["vuln_count"] = len(data)

        # Return success, no need to set the message, only the status
        # BaseConnector will create a textual message based off of the summary dictionary
        return action_result.set_status(phantom.APP_SUCCESS)

    def handle_action(self, param):
        ret_val = phantom.APP_SUCCESS

        # Get the action that we are supposed to execute for this App Run
        action_id = self.get_action_identifier()

        self.debug_print("action_id", self.get_action_identifier())

        if action_id == "test_connectivity":
            ret_val = self._handle_test_connectivity(param)

        elif action_id == "assign_tags":
            ret_val = self._handle_assign_tags(param)

        elif action_id == "list_tags":
            ret_val = self._handle_list_tags(param)

        elif action_id == "list_assets":
            ret_val = self._handle_list_assets(param)

        elif action_id == "list_agents":
            ret_val = self._handle_list_agents(param)

        elif action_id == "list_scan_results":
            ret_val = self._handle_list_scan_results(param)

        elif action_id == "list_scans":
            ret_val = self._handle_list_scans(param)

        elif action_id == "list_vulnerabilities":
            ret_val = self._handle_list_vulnerabilities(param)

        return ret_val

    def initialize(self):
        # Load the state in initialize, use it to store data
        # that needs to be accessed across actions
        self._state = self.load_state()

        # get the asset config
        config = self.get_config()
        """
        # Access values in asset config by the name

        # Required values can be accessed directly
        required_config_name = config["required_config_name"]

        # Optional values should use the .get() function
        optional_config_name = config.get("optional_config_name")
        """

        self._tio = TenableIO(
            config["access_key"],
            config["secret_key"],
            ssl_verify=config.get("verify_server_cert", True),
            vendor="Splunk",
            product="Phantom",
        )

        return phantom.APP_SUCCESS

    def finalize(self):
        # Save the state, this data is saved across actions and app upgrades
        self.save_state(self._state)
        return phantom.APP_SUCCESS


def main():
    import pudb
    import argparse

    pudb.set_trace()

    argparser = argparse.ArgumentParser()

    argparser.add_argument("input_test_json", help="Input Test JSON file")
    argparser.add_argument("-u", "--username", help="username", required=False)
    argparser.add_argument("-p", "--password", help="password", required=False)

    args = argparser.parse_args()
    session_id = None

    username = args.username
    password = args.password

    if username is not None and password is None:

        # User specified a username but not a password, so ask
        import getpass
        password = getpass.getpass("Password: ")

    if username and password:
        try:
            login_url = TenableioConnector._get_phantom_base_url() + "/login"

            print("Accessing the Login page")
            r = requests.get(login_url, verify=False)
            csrftoken = r.cookies["csrftoken"]

            data = dict()
            data["username"] = username
            data["password"] = password
            data["csrfmiddlewaretoken"] = csrftoken

            headers = dict()
            headers["Cookie"] = "csrftoken=" + csrftoken
            headers["Referer"] = login_url

            print("Logging into Platform to get the session id")
            r2 = requests.post(login_url, verify=False, data=data, headers=headers)
            session_id = r2.cookies["sessionid"]
        except Exception as e:
            print("Unable to get session id from the platform. Error: " + str(e))
            exit(1)

    with open(args.input_test_json) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print(json.dumps(in_json, indent=4))

        connector = TenableioConnector()
        connector.print_progress_message = True

        if session_id is not None:
            in_json["user_session_token"] = session_id
            connector._set_csrf_info(csrftoken, headers["Referer"])

        ret_val = connector._handle_action(json.dumps(in_json), None)
        print(json.dumps(json.loads(ret_val), indent=4))

    exit(0)


if __name__ == "__main__":
    main()
