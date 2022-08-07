import json
import logging
import requests

from marshmallow import fields

from O365_notifications.base import (
    O365BaseNotification,
    O365BaseSubscription,
    O365Notification,
    O365Subscriber,
    O365NotificationsHandler,
)

__all__ = (
    "O365KeepAliveNotification",
    "O365StreamingSubscription",
    "O365StreamingSubscriber",
)

logger = logging.getLogger(__name__)


class O365KeepAliveNotification(O365BaseNotification):
    status: str

    class O365KeepAliveNotificationSchema(O365BaseNotification.schema):
        status = fields.Str(data_key="Status")

    schema = O365KeepAliveNotificationSchema  # alias


class O365StreamingSubscription(O365BaseSubscription):

    class O365StreamingSubscriptionSchema(O365BaseSubscription.schema):
        context = fields.Str(data_key="@odata.context", load_only=True)
        url = fields.Str(data_key="@odata.id", load_only=True)

    schema = O365StreamingSubscriptionSchema  # alias


class O365StreamingSubscriber(O365Subscriber):
    _endpoints = {
        "subscriptions": "/subscriptions",
        "notifications": "/GetNotifications",
    }
    subscription_cls = O365StreamingSubscription

    def subscription_factory(self, **kwargs) -> O365StreamingSubscription:
        subscription_type = self.namespace.O365SubscriptionType.STREAMING_SUBSCRIPTION
        return self.subscription_cls(**{"type": subscription_type, **kwargs})

    def notification_factory(self, data) -> O365BaseNotification:
        base = O365BaseNotification.schema(namespace=self.namespace).load(**data)
        if base.type == self.namespace.O365NotificationType.NOTIFICATION:
            return O365Notification.deserialize(data, namespace=self.namespace)
        elif base.type == self.namespace.O365NotificationType.KEEP_ALIVE_NOTIFICATION:
            return O365KeepAliveNotification.deserialize(data, namespace=self.namespace)

    def create_event_channel(
        self,
        *,
        notification_handler: O365NotificationsHandler = None,
        connection_timeout: int = 120,  # equivalent to 2 hours
        keep_alive_interval: int = 5,  # in seconds
        refresh_after_expire: bool = False,
    ):
        """
        Create a new channel for events.

        :param notification_handler: the notification's handler
        :param connection_timeout: time in minutes in which connection closes
        :param keep_alive_interval: time interval in seconds in which a message is sent
        :param refresh_after_expire: refresh when http connection expires
        :raises ValueError: if no subscription is provided
        :raises Exception: if streaming error occurs
        """
        if not self.subscriptions:
            raise ValueError("can't start a streaming connection without subscription.")

        notification_handler = notification_handler or O365NotificationsHandler()
        url = self.build_url(self._endpoints.get("notifications"))

        request_schema = {
            "ConnectionTimeoutInMinutes": connection_timeout,
            "KeepAliveNotificationIntervalInSeconds": keep_alive_interval,
            "SubscriptionIds": [s.id for s in self.subscriptions],
        }

        logger.info("Open new events channel ...")
        while True:
            try:
                response = self.con.post(url, request_schema, stream=True)
                logger.debug("Start streaming cycle ...")

            # Renew subscriptions if 404 is raised
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == requests.codes.not_found:
                    logger.debug("Expired subscription.")
                    renewed_ids = [s.id for s in self.renew_subscriptions()]
                    request_schema["SubscriptionIds"] = renewed_ids
                    continue
                # raise for any other error
                raise e
            else:
                if not response:
                    return

            # Use 'with' clause to prevent requests.exceptions.ChunkedEncodingError.
            # Exception occurs when connection is closed by the server causing
            # partially reading the request body.
            with response:
                stream_data = b""
                bracket_control = []
                for starting_chunk in response.iter_content(chunk_size=1):
                    # Reading json group values...
                    if starting_chunk == b"[":
                        bracket_control.append(starting_chunk)
                        try:
                            for chunk in response.iter_content(chunk_size=1):
                                # Grouping json objects
                                if chunk == b"{":
                                    bracket_control.append(chunk)
                                elif chunk == b"}":
                                    bracket_control.remove(b"{")
                                elif chunk == b"]":
                                    bracket_control.remove(b"[")

                                # Control to see if json object is complete
                                if b"{" in bracket_control:
                                    stream_data += chunk
                                elif b"[" in bracket_control:
                                    if stream_data:
                                        stream_data += b"}"
                                        raw = json.loads(stream_data.decode("utf-8"))
                                        notification = self.notification_factory(raw)
                                        notification_handler.process(notification)
                                        stream_data = b""
                                else:
                                    # Break outer loop
                                    bracket_control.append(True)
                                    break  # Connection timed out

                        except Exception as e:
                            if isinstance(e, requests.exceptions.ChunkedEncodingError):
                                # Seem like empty values in the connection, is causing
                                # the communication to be corrupted. When that happens,
                                # the loop is interrupted and the streaming is restarted
                                logger.warning(f"Exception suppressed: {e}")
                                break
                            # raise for any other error
                            raise e
                    if bracket_control:
                        # Break loop since all data is read
                        break

            # Automatically refresh HTTP connection after it expires
            if refresh_after_expire:
                logger.debug("Refreshing connection ...")
            else:
                break

        logger.info("Stopped listening for events: connection closed.")
