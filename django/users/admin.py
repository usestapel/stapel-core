from django.contrib import admin
from stapel_core.django.admin import UserAdmin  # type: ignore
from .models import User

admin.site.register(User, UserAdmin)

