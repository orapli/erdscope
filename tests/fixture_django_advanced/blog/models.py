from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from geo.fields import PointField


class Tag(models.Model):
    # same class name as shop.Tag — both must survive as separate tables
    label = models.SlugField()


class Article(models.Model):
    title = models.CharField(max_length=200)
    # swappable target: statically unresolvable — the FK column must still
    # exist, only the edge is skipped
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)
    # custom field wrapper the parser doesn't know — skipped, not fatal
    location = PointField(srid=4326)


class Notice(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
