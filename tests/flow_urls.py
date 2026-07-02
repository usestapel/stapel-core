"""URLConf fixture for flow engine tests."""
from django.urls import path
from rest_framework.views import APIView


class DocumentedView(APIView):
    def post(self, request):  # pragma: no cover
        pass


class UndocumentedView(APIView):
    def get(self, request):  # pragma: no cover
        pass


urlpatterns = [
    path("api/documented/", DocumentedView.as_view()),
    path("api/undocumented/", UndocumentedView.as_view()),
]
