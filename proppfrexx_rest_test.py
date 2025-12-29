import requests
import time

filepath = (
    r"D:\Audio\Muzika\etceteral\Fofoulah - Seye (official video) [zRBW8n68nFw].mp3"
)

cmd = "http://localhost:8081/commands"
pw = ("Sample", "sample")
h = {"Content-Type": "text/plain"}


def post(name, command):
    request = requests.post(cmd, auth=pw, headers=h, data=command)

    print(f"{name} response: {request.text}")

    return request


get_next = post("Index of next track", "PLS_CURRENT_TRACKNEXT_GET")
num = int(get_next.text) - 1

print(f"Should be placed at {num}")

post("Append file", "PLS_CURRENT_APPEND_FILE " + filepath)
time.sleep(0.5)
post("Select last", "PLS_CURRENT_SELECT_ENTRY LAST")
time.sleep(0.5)
post("Move to next index", f"PLS_CURRENT_MOVE_TO {num}")
time.sleep(0.5)
post("Load track", "PLS_CURRENT_LOAD_SELECTED")

# ["SHOW_ALERT_WINDOW Test|Message 1"]
# ["SHOW_ALERT_WINDOW Test|Message 1", "SHOW_ALERT_WINDOW Test|Message 2"]
# curl -X GET -u Sample:sample -d 'MIXER_INPUT_VOLUME_GET MIC1' http://localhost:8081/commands

# curl -u Sample:sample -d "SHOW_ALERT_WINDOW Hello|Oh no!" http://localhost:8081/commands
# curl -u Sample:sample -d "PLS_CURRENT_APPEND_FILE wrong_file" http://localhost:8081/commands
# curl -u Sample:sample -d "PLS_CURRENT_PLAY_NEXT" http://localhost:8081/commands

# PLS_CURRENT_TRACKNEXT_GET
# PLS_CURRENT_MOVE_TO NUMBER
# PLS_CURRENT_LOAD_SELECTED
