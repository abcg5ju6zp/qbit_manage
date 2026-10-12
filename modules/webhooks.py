"""Class to handle webhooks."""

import json
import os
import time
import uuid

from modules import util
from modules.notification_store import DB_FILENAME
from modules.notification_store import OUTCOME_CONFIRMED
from modules.notification_store import OUTCOME_PERMANENT
from modules.notification_store import RECOVERY_TIMEOUT
from modules.notification_store import NotificationStore
from modules.notification_store import classify_response
from modules.notification_store import deliver_row
from modules.notification_store import payload_matches

logger = util.logger

GROUP_NOTIFICATION_LIMIT = 10
REQUEST_ATTEMPTS = 6
REQUEST_DELAY = 2


class Webhooks:
    """Class to handle webhooks."""

    def __init__(self, config, system_webhooks, notifiarr=None, apprise=None, web_api_used=False, run_id=None):
        """Initialize the class."""
        self.config = config
        self.error_webhooks = system_webhooks["error"] if "error" in system_webhooks else []
        self.run_start_webhooks = system_webhooks["run_start"] if "run_start" in system_webhooks else []
        self.run_end_webhooks = system_webhooks["run_end"] if "run_end" in system_webhooks else []
        if "function" in system_webhooks and system_webhooks["function"] is not None:
            try:
                self.function_webhooks = system_webhooks["function"][0]
            except (IndexError, KeyError):
                self.function_webhooks = []
        else:
            self.function_webhooks = []
        self.notifiarr = notifiarr
        self.apprise = apprise
        self.web_api_used = web_api_used
        self.run_id = run_id or (getattr(config, "args", None) or {}).get("run_id") or uuid.uuid4().hex
        function_hooks = self.function_webhooks
        self.enabled = bool(
            self.error_webhooks
            or self.run_start_webhooks
            or self.run_end_webhooks
            or (isinstance(function_hooks, dict) and any(function_hooks.values()))
        )
        self._store = None

    def _get_store(self):
        """Open the persistent outbox lazily; fall back to an in-memory one."""
        if self._store is not None:
            return self._store
        if not self.enabled:
            return None
        try:
            self._store = NotificationStore(os.path.join(self.config.default_dir, DB_FILENAME))
        except Exception as err:
            logger.error(f"Notification state store unavailable, retrying will not survive restarts ({err})")
            try:
                self._store = NotificationStore(":memory:")
            except Exception:
                self._store = None
        return self._store

    def _freeze_target(self, webhook):
        """Resolve a configured target to its kind and immutable delivery spec."""
        if webhook == "notifiarr":
            spec = {
                "url": self.notifiarr.url,
                "apikey": self.notifiarr.apikey,
                "instance": self.notifiarr.instance,
                "qbt_host": (self.config.data.get("qbt") or {}).get("host"),
            }
            return "notifiarr", spec
        if webhook == "apprise":
            spec = {"api_url": self.apprise.api_url, "notify_url": self.apprise.notify_url}
            return "apprise", spec
        return "webhook", {"url": webhook}

    def _resolve_targets(self, webhooks):
        """Return frozen ``(kind, spec)`` targets, preserving legacy guards."""
        targets = []
        for webhook in list(set(webhooks)):
            logger.trace(f"Webhook: {webhook}")
            if webhook is None:
                break
            elif (webhook == "notifiarr" and self.notifiarr is None) or (webhook == "apprise" and self.apprise is None):
                logger.warning(f"Webhook attribute set to {webhook} but {webhook} attribute is not configured.")
                break
            targets.append(self._freeze_target(webhook))
        return targets

    def _transmit_once(self, kind, spec, payload):
        """One live HTTP attempt through the config's retrying session helpers."""
        if kind == "webhook":
            response = self.config.post(spec["url"], json=payload)
        elif kind == "apprise":
            # Copy the payload: legacy code injected "urls" into the dict only
            # for the Apprise target, other targets must keep seeing the original.
            response = self.config.post(f"{spec['api_url']}/notify", json={**payload, "urls": spec["notify_url"]})
        else:
            response = self.config.get(
                f"{spec['url']}notification/qbitManage/",
                json=payload,
                headers={"X-API-Key": spec["apikey"]},
                params={"qbit_client": spec.get("qbt_host"), "instance": spec.get("instance")},
            )
            time.sleep(1)  # Pause for 1 second before sending the next request
        return response

    def _record_outcome(self, row, outcome, http_status, error):
        """Persist the per-target confirmation/failure and log it."""
        if outcome == OUTCOME_CONFIRMED:
            self._store.mark_confirmed(row["id"], http_status)
        elif outcome == OUTCOME_PERMANENT:
            logger.error(error)
            self._store.mark_permanent(row["id"], error, http_status)
        else:
            self._store.mark_temporary(row["id"], error, http_status)

    def _deliver_live(self, row):
        """Send a freshly recorded event, keeping the legacy 6x/2s retry loop."""
        if not payload_matches(row["payload_json"], row["payload_digest"]):
            error = "Notification payload integrity check failed; refusing to send altered content"
            logger.error(error)
            self._store.mark_permanent(row["id"], error)
            return
        spec = json.loads(row["target_json"])
        payload = json.loads(row["payload_json"])
        response = None
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                self._store.renew_lease(row["id"])
                response = self._transmit_once(row["target_kind"], spec, payload)
            except Exception as err:
                # Transport failures (incl. tenacity exhaustion) are uncertain:
                # leave unconfirmed so only this target is retried later.
                logger.warning(f"Webhook delivery failed ({type(err).__name__}); it will be retried")
                self._store.mark_temporary(row["id"], type(err).__name__)
                return
            if response.status_code < 500:
                break
            logger.debug(f"({response.status_code} [{response.reason}]) Retrying in {REQUEST_DELAY} seconds.")
            time.sleep(REQUEST_DELAY)
            logger.debug(f"(Retry {attempt + 1} of {REQUEST_ATTEMPTS}.")
        else:
            logger.warning(f"({response.status_code} [{response.reason}]) after {REQUEST_ATTEMPTS} attempts.")
        self._record_outcome(row, *classify_response(row["target_kind"], response))

    def _request(self, webhooks, json, event_type, event_function=None):
        """
        Persist the event per target, then deliver. Each target is confirmed
        independently: a permanent rejection is recorded and queryable, while a
        temporary failure stays pending and is retried without resending to the
        targets that already confirmed.
        """
        logger.trace("")
        logger.trace(f"JSON: {json}")
        targets = self._resolve_targets(webhooks)
        if not targets:
            return
        store = self._get_store()
        if store is None:
            return
        _, row_ids = store.add_event(self.run_id, event_type, targets, json, event_function=event_function)
        for row in store.get_rows(row_ids):
            self._deliver_live(row)

    def recover_pending(self):
        """Retry events left unconfirmed by a prior run, crash or restart.

        Uses the target settings frozen on each row — current config is never
        consulted, so removed/changed targets (including rotated keys) do not
        rewrite an old event. One bounded HTTP attempt per target; the normal
        backoff schedule decides the next try.
        """
        if not self.enabled or self.config.args.get("validation_mode", False):
            return
        store = self._get_store()
        if store is None:
            return
        store.prune()
        for row in store.claim_due():
            if not payload_matches(row["payload_json"], row["payload_digest"]):
                error = "Notification payload integrity check failed; refusing to send altered content"
                logger.error(error)
                store.mark_permanent(row["id"], error)
                continue
            outcome, http_status, error = deliver_row(row, self.config.session, timeout=RECOVERY_TIMEOUT)
            self._record_outcome(row, outcome, http_status, error)
            if row["target_kind"] == "notifiarr":
                time.sleep(1)  # match the Notifiarr API pacing used on the live path

    def flush_pending(self):
        """Drain due pending events at the end of a run (see recover_pending)."""
        self.recover_pending()

    def start_time_hooks(self, start_time):
        """Send a webhook to notify that the run has started."""
        # Skip sending start notifications during config validation
        if self.run_start_webhooks and not self.config.args.get("validation_mode", False):
            dry_run = self.config.commands["dry_run"]
            if dry_run:
                start_type = "Dry-"
            else:
                start_type = ""

            # Get enabled commands
            enabled_commands = [
                cmd
                for cmd in [
                    "recheck",
                    "cat_update",
                    "tag_update",
                    "rem_unregistered",
                    "tag_tracker_error",
                    "rem_orphaned",
                    "tag_nohardlinks",
                    "share_limits",
                ]
                if self.config.commands.get(cmd, False)
            ]

            # Get execution options
            execution_options = [
                opt for opt in ["skip_cleanup", "dry_run", "skip_qb_version_check"] if self.config.commands.get(opt, False)
            ]

            self._request(
                self.run_start_webhooks,
                {
                    "function": "run_start",
                    "title": None,
                    "body": f"Starting {'WebAPI ' if self.web_api_used else ''}{start_type}Run",
                    "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "dry_run": self.config.commands["dry_run"],
                    "web_api_used": self.web_api_used,
                    "commands": enabled_commands,
                    "execution_options": execution_options,
                },
                "run_start",
            )

    def end_time_hooks(self, start_time, end_time, run_time, next_run, stats, body):
        """Send a webhook to notify that the run has ended."""
        if self.run_end_webhooks:
            self._request(
                self.run_end_webhooks,
                {
                    "function": "run_end",
                    "title": None,
                    "body": body,
                    "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run is not None else next_run,
                    "run_time": run_time,
                    "torrents_added": stats["added"],
                    "torrents_deleted": stats["deleted"],
                    "torrents_deleted_and_contents_count": stats["deleted_contents"],
                    "torrents_resumed": stats["resumed"],
                    "torrents_rechecked": stats["rechecked"],
                    "torrents_categorized": stats["categorized"],
                    "torrents_tagged": stats["tagged"],
                    "remove_unregistered": stats["rem_unreg"],
                    "torrents_tagged_tracker_error": stats["tagged_tracker_error"],
                    "torrents_untagged_tracker_error": stats["untagged_tracker_error"],
                    "orphaned_files_found": stats["orphaned"],
                    "torrents_tagged_no_hardlinks": stats["tagged_noHL"],
                    "torrents_untagged_no_hardlinks": stats["untagged_noHL"],
                    "torrents_updated_share_limits": stats["updated_share_limits"],
                    "torrents_cleaned_share_limits": stats["cleaned_share_limits"],
                    "files_deleted_from_recyclebin": stats["recycle_emptied"],
                    "files_deleted_from_orphaned": stats["orphaned_emptied"],
                },
                "run_end",
            )

    def error_hooks(self, text, function_error=None, critical=True):
        """Send a webhook to notify that an error has occurred."""
        if self.error_webhooks:
            err_type = "failure" if critical is True else "warning"
            json = {
                "function": "run_error",
                "title": f"{function_error} Error",
                "body": str(text),
                "critical": critical,
                "type": err_type,
            }
            if function_error:
                json["function_error"] = function_error
            self._request(self.error_webhooks, json, "run_error", event_function=function_error)

    def function_hooks(self, webhook, json):
        """Send a webhook to notify that a function has completed."""
        if self.function_webhooks:
            self._request(webhook, json, "function", event_function=json.get("function"))

    def notify(self, torrents_updated=[], payload={}, group_by=""):
        if len(torrents_updated) > GROUP_NOTIFICATION_LIMIT:
            logger.trace(
                f"Number of torrents updated > {GROUP_NOTIFICATION_LIMIT}, grouping notifications"
                f"{f' by {group_by}' if group_by else ''}",
            )
            if group_by == "category":
                group_attr = group_notifications_by_key(payload, "torrent_category")
            elif group_by == "tag":
                group_attr = group_notifications_by_key(payload, "torrent_tag")
            elif group_by == "tracker":
                group_attr = group_notifications_by_key(payload, "torrent_tracker")
            elif group_by == "status":
                group_attr = group_notifications_by_key(payload, "torrent_status")

            # group notifications by grouping attribute
            for group in group_attr:
                num_torrents_updated = len(group_attr[group]["torrents"])
                only_one_torrent_updated = num_torrents_updated == 1

                attr = {
                    "function": group_attr[group]["function"],
                    "title": f"{group_attr[group]['title']} for {group}",
                    "body": (
                        group_attr[group]["body"]
                        if only_one_torrent_updated
                        else (
                            f"Updated {num_torrents_updated} "
                            f"{'torrent' if only_one_torrent_updated else 'torrents'} with {group_by} '{group}'"
                        )
                    ),
                    "torrents": group_attr[group]["torrents"],
                }
                if group_by == "category":
                    attr["torrent_category"] = group
                    attr["torrent_tag"] = group_attr[group].get("torrent_tag") if only_one_torrent_updated else None
                    attr["torrent_tracker"] = group_attr[group].get("torrent_tracker") if only_one_torrent_updated else None
                    attr["notifiarr_indexer"] = group_attr[group].get("notifiarr_indexer") if only_one_torrent_updated else None
                elif group_by == "tag":
                    attr["torrent_tag"] = group
                    attr["torrent_category"] = group_attr[group].get("torrent_category") if only_one_torrent_updated else None
                    attr["torrent_tracker"] = group_attr[group].get("torrent_tracker")
                    attr["notifiarr_indexer"] = group_attr[group].get("notifiarr_indexer")
                elif group_by == "tracker":
                    attr["torrent_tracker"] = group
                    attr["torrent_category"] = group_attr[group].get("torrent_category") if only_one_torrent_updated else None
                    attr["torrent_tag"] = group_attr[group].get("torrent_tag") if only_one_torrent_updated else None
                    attr["notifiarr_indexer"] = group_attr[group].get("notifiarr_indexer")
                elif group_by == "status":
                    attr["torrent_status"] = group
                    attr["torrent_category"] = group_attr[group].get("torrent_category")
                    attr["torrent_tag"] = group_attr[group].get("torrent_tag")
                    attr["torrent_tracker"] = group_attr[group].get("torrent_tracker")
                    attr["notifiarr_indexer"] = group_attr[group].get("notifiarr_indexer")

                self.config.send_notifications(attr)
        else:
            for attr in payload:
                self.config.send_notifications(attr)


def group_notifications_by_key(payload, key):
    """Group notifications by key"""
    group_attr = {}
    for attr in payload:
        group = attr[key]
        if group not in group_attr:
            group_attr[group] = attr
        else:
            group_attr[group]["torrents"].append(attr.get("torrents", [None])[0])
    return group_attr
