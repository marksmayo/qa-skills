import json


class InsurerClient:
    """Looks up a member's cover. Tests replay a recorded response instead of calling the insurer."""

    def __init__(self, transport):
        self.transport = transport

    def cover(self, member_ref):
        response = self.transport("GET", f"/members/{member_ref}/cover")
        return json.loads(response["body"])["cover"]
