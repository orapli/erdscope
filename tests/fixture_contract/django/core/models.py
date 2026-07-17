from django.db import models


class User(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        db_table = 'users'


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    class Meta:
        db_table = 'profiles'


class Post(models.Model):
    title = models.CharField(max_length=200)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True)
    tags = models.ManyToManyField('Tag')

    class Meta:
        db_table = 'posts'


class Tag(models.Model):
    label = models.SlugField()

    class Meta:
        db_table = 'tags'
