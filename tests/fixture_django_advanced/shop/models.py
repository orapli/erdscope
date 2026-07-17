from django.db import models


class Tag(models.Model):
    # same class name as blog.Tag
    name = models.CharField(max_length=50)


class Item(models.Model):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)          # own app's Tag
    blog_tag = models.ForeignKey('blog.Tag', on_delete=models.CASCADE)
