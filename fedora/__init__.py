from typing import Type, Union
import inspect
import re
import datetime
import pytz
import requests
import httpx
from urllib.parse import urljoin

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

import fasjson_client

from maubot import Plugin, MessageEvent
from maubot.handlers import command


NL = "      \n"

ALIASES = {"hello": ["hi", "hello2", "hellomynameis"], "user": ["fasinfo"]}


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("fasjson_url")
        helper.copy("accounts_baseurl")
        helper.copy("pagure_url")

class InfoGatherError(Exception):
    def __init__(self, message):
        self.message = message

class Fedora(Plugin):
    def _userlink(self, username: str) -> str:
        """
        Returns a markdown string with the link to the accounts
        for that user
        """
        return f"[{username}]({self.config['accounts_baseurl']}user/{username})"

    def catch_generic_fasjson_errors(func):
        def wrapper(self, *args, **kwargs):
            if self.fasjsonclient is None:
                # if the connection to FASJSON failed at plugin start, fasjsonclient will be None
                raise InfoGatherError(f"Sorry, I can not give you the required information. I failed to connect to FASJSON on startup")
            try:
                return func(self, *args, **kwargs)
            except fasjson_client.errors.ClientSetupError as e:
                # typically this happens after the plugin starts up and runs for a while i.e. kerb ticket expires
                raise InfoGatherError(f"Sorry, I can not give you the required information. I failed to connect to FASJSON: **{e}**")
            # except fasjson_client.errors.APIError as e:
            # return f"Sorry, I can not give you the required information. FASJSON returned API Error: **{e}**"

        return wrapper

    @catch_generic_fasjson_errors
    def _get_fasuser(self, username: str, evt: MessageEvent):
        def _get_person_by_username(username: str) -> dict | str:
            """looks up a user by the username"""
            try:
                person = self.fasjsonclient.get_user(username=username).result
            except fasjson_client.errors.APIError as e:
                if e.code == 404:
                    raise InfoGatherError(f"Sorry, but Fedora Accounts user '{username}' does not exist")
            return person

        def _get_users_by_matrix_id(username: str) -> Union[dict, str]:
            """looks up a user by the matrix id"""

            # Fedora Accounts stores these strangly but this is to handle that
            try:
                matrix_username, matrix_server = re.findall(r"@(.*):(.*)", username)[0]
            except ValueError or IndexError:
                raise InfoGatherError(f"Sorry, {username} does not look like a valid matrix user ID (e.g. @username:homeserver.com )")

            # if given a fedora.im address -- just look up the username as a FAS name
            if matrix_server == "fedora.im":
                return _get_person_by_username(matrix_username)

            searchterm = f"matrix://{matrix_server}/{matrix_username}"
            searchresult = self.fasjsonclient.search(ircnick__exact=searchterm).result

            if len(searchresult) > 1:
                names = f"{NL}".join(name for name in searchresult)
                raise InfoGatherError(
                    f"{len(searchresult)} Fedora Accounts users have the {username} Matrix Account defined:{NL}"
                    f"{names}"
                )
            elif len(searchresult) == 0:
                raise InfoGatherError(f"No Fedora Accounts users have the {username} Matrix Account defined")

            return searchresult[0]

        # if no username is supplied, we use the matrix id of the sender (e.g. "@dudemcpants:fedora.im")
        if not username:
            username = evt.sender

        # check if the formatted message has mentions (ie the user has tab-completed on someones
        # name) in them
        if evt.content.formatted_body:
            # in element at least, when usernames are mentioned, they are formatted like:
            # <a href="https://matrix.to/#/@zodbot:fedora.im">zodbot</a>
            # here we check the formatted message and extract all the matrix user IDs
            u = re.findall(
                r'href=[\'"]?http[s]?://matrix.to/#/([^\'" >]+)',
                evt.content.formatted_body,
            )
            if len(u) > 1:
                raise InfoGatherError("Sorry, I can only look up one username at a time")
            elif len(u) == 1:
                return _get_users_by_matrix_id(u[0])

        usernames = username.split(" ")
        if len(usernames) > 1:
            raise InfoGatherError("Sorry, I can only look up one username at a time")

        # else check if the username given is a matrix id (@<username>:<server.com>)
        if re.search(r"@.*:.*", usernames[0]):
            return _get_users_by_matrix_id(usernames[0])

        # finally, assume we were given a FAS / Fedora Account ID and use that
        else:
            return _get_person_by_username(usernames[0])

    @catch_generic_fasjson_errors
    def _get_group_members(self, groupname: str) -> Union[list, str]:
        """looks up a group members by the groupname"""
        try:
            members = self.fasjsonclient.list_group_members(groupname=groupname).result
        except fasjson_client.errors.APIError as e:
            if e.code == 404:
                raise InfoGatherError(f"Sorry, but group '{groupname}' does not exist")
        return members

    @catch_generic_fasjson_errors
    def _get_group_sponsors(self, groupname: str) -> Union[list, str]:
        """looks up a group sponsors by the groupname"""
        try:
            sponsors = self.fasjsonclient.list_group_sponsors(
                groupname=groupname
            ).result
        except fasjson_client.errors.APIError as e:
            if e.code == 404:
                raise InfoGatherError(f"Sorry, but group '{groupname}' does not exist")
        return sponsors
    
    async def _get_pagure_issue(self, project, issue_id, namespace=''):
        endpoint = self.pagure_url + "/".join([namespace, project, 'issue', issue_id])
        async with httpx.AsyncClient() as client:
            response = await client.get(endpoint)
        json = response.json()
        if response.status_code == 404:
            error_code = json.get('error_code')
            error = json.get('error')
            if error_code == 'ENOPROJECT':
                raise InfoGatherError(f"Project {project} not found")
            elif error_code == 'ENOISSUE':
                raise InfoGatherError(f"Issue #{issue_id} not found on {project} project")
            else:
                raise InfoGatherError(f"Issue querying Pagure: {error_code}: {error}")
        elif response.status_code != 200:
            raise InfoGatherError(f"Issue querying Pagure: {response.status_code}: {response.reason_phrase}")
        return json

    async def start(self) -> None:
        self.config.load_and_update()
        self.pagure_url = self.config["pagure_url"]
        try:
            self.fasjsonclient = fasjson_client.Client(
                url=self.config["fasjson_url"],
            )
        except fasjson_client.errors.ClientSetupError as e:
            self.fasjsonclient = None
            self.log.error(
                "Something went wrong setting up " "fasjson client with error: %s" % e
            )

    async def stop(self) -> None:
        pass

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @command.new(name="help", help=f"list commands")
    @command.argument("commandname", pass_raw=True, required=False)
    async def bothelp(self, evt: MessageEvent, commandname: str) -> None:
        """return help"""

        if commandname:
            # return the full help (docstring) for the given command
            for c, commandobject in inspect.getmembers(self):
                if commandname == c or commandname in ALIASES.get(c, []):
                    output = f"{commandobject.__mb_full_help__}{NL}{inspect.getdoc(commandobject.__mb_func__)}"
                    aliases = ALIASES.get(commandname, []) or ALIASES.get(c, [])
                    if aliases:
                        output = f"{output}{NL}{NL}#### Aliases ####{NL}"
                        for alias in aliases:
                            output = f"{output}* `{alias}`{NL}"
                    await evt.respond(output)
                    return

            await evt.reply(f"`{commandname}` is not a valid command")
            return
        else:
            # list all the commands with the help arg from command.new
            output = ""
            for c, commandobject in inspect.getmembers(self):
                if isinstance(commandobject, command.CommandHandler):
                    # generate arguments string
                    arguments = ""
                    for argument in commandobject.__mb_arguments__:
                        if argument.required:
                            arguments = f"{arguments} <{argument.name}>"
                        else:
                            arguments = f"{arguments} [{argument.name}]"
                    output = (
                        output
                        + f"`!{commandobject.__mb_name__} {arguments}`:: {commandobject.__mb_help__}      \n"
                    )
            await evt.respond(output)

    @command.new(help="return information about this bot")
    async def version(self, evt: MessageEvent) -> None:
        """
        Return the version of the plugin

        Takes no arguments
        """

        await evt.respond(f"maubot-fedora version {self.loader.meta.version}")

    @command.new(help="Return a list of members of the specified group")
    @command.argument("groupname", pass_raw=True, required=True)
    async def members(self, evt: MessageEvent, groupname: str) -> None:
        """
        Return a list of the members of the Fedora Accounts group

        ## Arguments ##
        `groupname`: (required) The name of the Fedora Accounts group
        """
        if not groupname:
            await evt.respond(
                "groupname argument is required. e.g. `!members designteam`"
            )
            return

        try:
            members = self._get_group_members(groupname)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return            
        
        if len(members) > 200:
            await evt.respond(f"{groupname} has {len(members)} and thats too much to dump here")
        else:
            await evt.respond(
            f"Members of {groupname}: {', '.join(self._userlink(m['username']) for m in members)}"
        )

    @command.new(help="Return a list of owners of the specified group")
    @command.argument("groupname", pass_raw=True, required=True)
    async def sponsors(self, evt: MessageEvent, groupname: str) -> None:
        if not groupname:
            await evt.respond(
                "groupname argument is required. e.g. `!sponsors designteam`"
            )
            return

        try:
            sponsors = self._get_group_sponsors(groupname)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return

        await evt.respond(
            f"Sponsors of {groupname}: {', '.join(self._userlink(s['username']) for s in sponsors)}"
        )

    @command.new(
        help="Return brief information about a Fedora user.", aliases=ALIASES["hello"]
    )
    @command.argument("username", pass_raw=True, required=False)
    async def hello(self, evt: MessageEvent, username: str) -> None:
        """
        Returns a short line of information about the user. If no username is provided, defaults to
        the sender of the message.

        #### Arguments ####

        * `username`: A Fedora Accounts username or a Matrix User ID
           (e.g. @username:fedora.im)

        """
        try:
            user = self._get_fasuser(username, evt)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return

        message = f"{user['human_name']} ({user['username']})"
        if pronouns := user.get('pronouns'):
            message += ' - ' + ' or '.join(pronouns)
        await evt.respond(message)

    @command.new(
        help="Return brief information about a Fedora user.", aliases=ALIASES["user"]
    )
    @command.argument("username", pass_raw=True, required=False)
    async def user(self, evt: MessageEvent, username: str) -> None:
        """
        Returns a information from Fedora Accounts about the user
        If no username is provided, defaults to the sender of the message.

        #### Arguments ####

        * `username`: A Fedora Accounts username or a Matrix User ID
           (e.g. @username:fedora.im)

        """
        try:
            user = self._get_fasuser(username, evt)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return

        await evt.respond(
            f"User: {user.get('username')},{NL}"
            f"Name: {user.get('human_name')},{NL}"
            f"Pronouns: {' or '.join(user.get('pronouns') or [])},{NL}"
            f"Creation: {user.get('creation')},{NL}"
            f"Timezone: {user.get('timezone')},{NL}"
            f"Locale: {user.get('locale')},{NL}"
            f"GPG Key IDs: {' and '.join(k for k in user['gpgkeyids'] or ['None'])},{NL}"
        )

    @command.new(help="Returns the current time of the user.")
    @command.argument("username", pass_raw=True, required=False)
    async def localtime(self, evt: MessageEvent, username: str) -> None:
        """
        Returns the current time of the user.
        The timezone is queried from FAS.

        #### Arguments ####

        * `username`: A Fedora Accounts username or a Matrix User ID
           (e.g. @username:fedora.im)

        """
        try:
            user = self._get_fasuser(username, evt)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return

        timezone_name = user["timezone"]
        if timezone_name is None:
            await evt.reply(
                'User "%s" doesn\'t share their timezone' % user.get("username")
            )
            return
        try:
            time = datetime.datetime.now(pytz.timezone(timezone_name))
        except Exception:
            await evt.reply(
                'The timezone of "%s" was unknown: "%s"'
                % (user.get("username"), timezone_name)
            )
            return
        await evt.respond(
            'The current local time of "%s" is: "%s" (timezone: %s)'
            % (user.get("username"), time.strftime("%H:%M"), timezone_name)
        )

    @command.new(help="Retrieve the owner of a given package")
    @command.argument("package", required=True)
    async def whoowns(self, evt: MessageEvent, package: str) -> None:
        """
        Retrieve the owner of a given package

        #### Arguments ####

        * `package`: A Fedora package name

        """
        # First use pagure info
        url = "https://src.fedoraproject.org/api/0/rpms/"
        req = requests.get(url + package)
        if req.status_code == 404:
            await evt.reply("Package %s not found." % package)
            return

        req_json = req.json()
        admins = ", ".join(req_json["access_users"]["admin"])
        owners = ", ".join(req_json["access_users"]["owner"])
        committers = ", ".join(req_json["access_users"]["commit"])

        if owners:
            owners = f"__owner:__ {owners}{NL}"
        if admins:
            admins = f"__admin:__ {admins}{NL}"
        if committers:
            committers = f"__commit:__ {committers}{NL}"

        resp = "".join([x for x in [owners, admins, committers] if x != ""])

        await evt.respond(resp)

    @command.new(help="return a pagure issue")
    @command.argument("project", required=True)
    @command.argument("issue_id", required=True)
    async def pagureissue(self, evt: MessageEvent, project: str, issue_id: str) -> None:
        """
        Show a summary of a Pagure issue

        #### Arguments ####

        * `project`: a project in pagure.io
        * `issue_id`: the issue number

        """
        try:
            issue = await self._get_pagure_issue(project, issue_id)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return
        title = issue.get('title')
        full_url = issue.get('full_url')
        await evt.respond(f"[{project} #{issue_id}]({full_url}): {title}")
    
    # these were done in supybot / limnoria with the alias plugin. need to find a better way to
    # do this so they can be defined, but for now, lets just define the commands here.
    @command.new(help="Get a Summary of a ticket from the packaging-committee ticket tracker")
    @command.argument("issue_id", required=True)
    async def fpc(self, evt: MessageEvent, issue_id: str) -> None:
        """
        Show a summary of an issue in the `packaging-committee` pagure.io project

        #### Arguments ####

        * `issue_id`: the issue number

        """
        try:
            issue = await self._get_pagure_issue("packaging-committee", issue_id)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return
        title = issue.get('title')
        full_url = issue.get('full_url')
        await evt.respond(f"[packaging-committee #{issue_id}]({full_url}): {title}")

    # these were done in supybot / limnoria with the alias plugin. need to find a better way to
    # do this so they can be defined, but for now, lets just define the commands here.
    @command.new(help="Get a Summary of a ticket from the epel ticket tracker")
    @command.argument("issue_id", required=True)
    async def epel(self, evt: MessageEvent, issue_id: str) -> None:
        """
        Show a summary of an issue in the `epel` pagure.io project

        #### Arguments ####

        * `issue_id`: the issue number

        """
        try:
            issue = await self._get_pagure_issue("epel", issue_id)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return
        title = issue.get('title')
        full_url = issue.get('full_url')
        await evt.respond(f"[epel #{issue_id}]({full_url}): {title}")

    # these were done in supybot / limnoria with the alias plugin. need to find a better way to
    # do this so they can be defined, but for now, lets just define the commands here.
    @command.new(help="Get a Summary of a ticket from the fesco ticket tracker")
    @command.argument("issue_id", required=True)
    async def fesco(self, evt: MessageEvent, issue_id: str) -> None:
        """
        Show a summary of an issue in the `fesco` pagure.io project

        #### Arguments ####

        * `issue_id`: the issue number

        """
        try:
            issue = await self._get_pagure_issue("fesco", issue_id)
        except InfoGatherError as e:
            await evt.respond(e.message)
            return
        title = issue.get('title')
        full_url = issue.get('full_url')
        await evt.respond(f"[fesco #{issue_id}]({full_url}): {title}")
