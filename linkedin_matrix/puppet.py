import magic
import re
from datetime import datetime, timedelta
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterable,
    Awaitable,
    cast,
    Dict,
    List,
    Optional,
    TYPE_CHECKING,
    Union,
)

import requests
from mautrix.appservice import IntentAPI
from mautrix.bridge import async_getter_lock, BasePuppet
from mautrix.types import ContentURI, UserID, RoomID, SyncToken
from mautrix.util.simple_template import SimpleTemplate
from yarl import URL

from .config import Config
from .db import Puppet as DBPuppet
from . import user as u, portal as p, matrix as m

if TYPE_CHECKING:
    from .__main__ import LinkedInBridge


class Puppet(DBPuppet, BasePuppet):
    mx: m.MatrixHandler
    config: Config
    hs_domain: str
    mxid_template: SimpleTemplate[str]

    by_li_member_urn: Dict[str, "Puppet"] = {}
    by_custom_mxid: Dict[UserID, "Puppet"] = {}

    def __init__(
        self,
        li_member_urn: str,
        name: Optional[str] = None,
        photo_id: Optional[str] = None,
        photo_mxc: Optional[ContentURI] = None,
        name_set: bool = False,
        avatar_set: bool = False,
        is_registered: bool = False,
        custom_mxid: Optional[UserID] = None,
        next_batch: Optional[SyncToken] = None,
    ):
        super().__init__(
            li_member_urn,
            name,
            photo_id,
            photo_mxc,
            custom_mxid,
            next_batch,
            name_set,
            avatar_set,
            is_registered,
        )
        self._last_info_sync: Optional[datetime] = None

        # TODO this is where I should convert to a proper MXID
        self.default_mxid = self.get_mxid_from_id(li_member_urn)
        self.default_mxid_intent = self.az.intent.user(self.default_mxid)
        self.intent = self._fresh_intent()

        self.log = self.log.getChild(self.li_member_urn)

    @classmethod
    def init_cls(cls, bridge: "LinkedInBridge") -> AsyncIterable[Awaitable[None]]:
        cls.config = bridge.config
        cls.loop = bridge.loop
        cls.mx = bridge.matrix
        cls.az = bridge.az
        cls.hs_domain = cls.config["homeserver.domain"]
        cls.mxid_template = SimpleTemplate(
            cls.config["bridge.username_template"],
            "userid",
            prefix="@",
            suffix=f":{Puppet.hs_domain}",
            type=str,
        )
        cls.sync_with_custom_puppets = cls.config["bridge.sync_with_custom_puppets"]
        cls.homeserver_url_map = {
            server: URL(url)
            for server, url in cls.config["bridge.double_puppet_server_map"].items()
        }
        cls.allow_discover_url = cls.config["bridge.double_puppet_allow_discovery"]
        cls.login_shared_secret_map = {
            server: secret.encode("utf-8")
            for server, secret in cls.config["bridge.login_shared_secret_map"].items()
        }
        cls.login_device_name = "LinkedIn Messages Bridge"

        return (
            puppet.try_start() async for puppet in Puppet.get_all_with_custom_mxid()
        )

    def intent_for(self, portal: "p.Portal") -> IntentAPI:
        if portal.li_other_user_urn == self.li_member_urn or (
            portal.backfill_lock.locked
            and self.config["bridge.backfill.invite_own_puppet"]
        ):
            return self.default_mxid_intent
        return self.intent

    # region User info updating

    async def update_info(
        self,
        source: Optional[u.User],
        info: Dict[str, Any] = None,
        update_avatar: bool = True,
    ) -> "Puppet":
        if not info:
            # TODO fetch the user info directly from the API?
            return self

        self._last_info_sync = datetime.now()
        try:
            changed = await self._update_name(info)
            if update_avatar:
                changed = (
                    await self._update_photo(
                        info.get("miniProfile", {})
                        .get("picture", {})
                        .get("com.linkedin.common.VectorImage", {}),
                    )
                    or changed
                )

            if changed:
                await self.save()
        except Exception:
            self.log.exception(
                f"Failed to update info from source {source.li_member_urn}"
            )
        return self

    @staticmethod
    async def reupload_avatar(intent: IntentAPI, url: str) -> ContentURI:
        image_data = requests.get(url)
        if not image_data.ok:
            raise Exception("Couldn't download profile picture")

        mime = magic.from_buffer(image_data.content, mime=True)
        return await intent.upload_media(image_data.content, mime_type=mime)

    async def _update_name(self, info: Dict[str, Any]) -> bool:
        name = self._get_displayname(info)
        if name != self.name or not self.name_set:
            self.name = name
            try:
                await self.default_mxid_intent.set_displayname(self.name)
                self.name_set = True
            except Exception:
                self.log.exception("Failed to set displayname")
                self.name_set = False
            return True
        return False

    @classmethod
    def _get_displayname(cls, info: Dict[str, Any]) -> str:
        profile = info.get("miniProfile", {})
        first, last = profile.get("firstName"), profile.get("lastName")
        info = {
            "displayname": None,
            "name": f"{first} {last}",
            "first_name": first,
            "last_name": last,
        }
        for preference in cls.config["bridge.displayname_preference"]:
            if info.get(preference):
                info["displayname"] = info.get(preference)
                break
        return cls.config["bridge.displayname_template"].format(**info)

    photo_id_re = re.compile(r"https://.*?/image/(.*?)/profile-.*?")

    async def _update_photo(self, picture: Dict[str, Any]) -> bool:
        root_url = picture.get("rootUrl")
        photo_id = None
        if root_url:
            match = self.photo_id_re.match(root_url)
            if match:
                photo_id = match.group(1)

        if photo_id != self.photo_id or not self.avatar_set:
            self.photo_id = photo_id

            if photo_id:
                # Use the 100x100 image
                file_path_segment = picture["artifacts"][0][
                    "fileIdentifyingUrlPathSegment"
                ]

                self.photo_mxc = await self.reupload_avatar(
                    self.default_mxid_intent,
                    root_url + file_path_segment,
                )
            else:
                self.photo_mxc = ContentURI("")

            try:
                await self.default_mxid_intent.set_avatar_url(self.photo_mxc)
                self.avatar_set = True
            except Exception:
                self.log.exception("Failed to set avatar")
                self.avatar_set = False

            return True
        return False

    # endregion

    # region Database getters

    def _add_to_cache(self) -> None:
        self.by_li_member_urn[self.li_member_urn] = self
        if self.custom_mxid:
            self.by_custom_mxid[self.custom_mxid] = self

    @classmethod
    @async_getter_lock
    async def get_by_li_member_urn(
        cls,
        li_member_urn: str,
        *,
        create: bool = True,
    ) -> Optional["Puppet"]:
        try:
            return cls.by_li_member_urn[li_member_urn]
        except KeyError:
            pass

        puppet = cast(cls, await super().get_by_li_member_urn(li_member_urn))
        if puppet:
            puppet._add_to_cache()
            return puppet

        if create:
            puppet = cls(li_member_urn, None, None, None, False, False)
            await puppet.insert()
            puppet._add_to_cache()
            return puppet

        return None

    @classmethod
    async def get_by_mxid(cls, mxid: UserID, create: bool = True) -> Optional["Puppet"]:
        # TODO and here (on the conversion back)
        li_member_urn = cls.get_id_from_mxid(mxid)
        if li_member_urn:
            return await cls.get_by_li_member_urn(li_member_urn, create=create)
        return None

    @classmethod
    @async_getter_lock
    async def get_by_custom_mxid(cls, mxid: UserID) -> Optional["Puppet"]:
        try:
            return cls.by_custom_mxid[mxid]
        except KeyError:
            pass

        puppet = cast("Puppet", await super().get_by_custom_mxid(mxid))
        if puppet:
            puppet._add_to_cache()
            return puppet

        return None

    @classmethod
    async def get_all_with_custom_mxid(cls) -> AsyncGenerator["Puppet", None]:
        puppets = await super().get_all_with_custom_mxid()
        print("get_all_with_custom_mxid", puppets)
        for puppet in cast(List[Puppet], puppets):
            try:
                yield cls.by_li_member_urn[puppet.li_member_urn]
            except KeyError:
                puppet._add_to_cache()
                yield puppet

    # TODO which involse these two functions
    @classmethod
    def get_id_from_mxid(cls, mxid: UserID) -> Optional[str]:
        return cls.mxid_template.parse(mxid)

    @classmethod
    def get_mxid_from_id(cls, li_member_urn: str) -> UserID:
        return UserID(cls.mxid_template.format_full(li_member_urn))

    # endregion
