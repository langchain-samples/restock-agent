from managed_deepagents import auth, define_identity

# Studio signs each person in; the API key identifies a client for custom frontends.
identity = define_identity(auth=auth.langsmith_api_key())
