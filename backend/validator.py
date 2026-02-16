def validate_blueprint(bp):

    if bp["environment"] == "prod":
        if "t2.micro" in str(bp):
            raise Exception("Unsafe instance for production")
