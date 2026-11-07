import re


def login_form(client, username, password):
    page = client.get("/login", follow_redirects=False)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return {"username": username, "password": password, "csrf_token": token}
