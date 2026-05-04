from app.schemas.profile import Profile


def get_profile() -> Profile:
    return Profile(
        name="Иван Ерохин",
        occupation="Разработчик",
        interests=["Python", "Go", "веб-разработка", "DIY", "Smart Home"],
        bio="Люблю писать код",
    )
