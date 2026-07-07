import asyncio

from asyncua import ua

from opcua_client import OPCUAController


class BadNode:
    async def read_value(self):
        raise ua.UaStatusCodeError(ua.StatusCodes.BadNodeIdUnknown)


class FakeClient:
    def get_node(self, node_id):
        return BadNode()


async def run_bad_node_read():
    controller = OPCUAController("opc.tcp://example:6810")
    controller.client = FakeClient()
    controller._connected = True
    value = await controller.read_value("ns=10011;s=AC_AVC01.DV")
    return controller, value


def test_bad_node_id_does_not_mark_opcua_connection_disconnected():
    controller, value = asyncio.run(run_bad_node_read())
    assert value is None
    assert controller.connected is True


if __name__ == "__main__":
    test_bad_node_id_does_not_mark_opcua_connection_disconnected()
    print("opcua client tests passed")
