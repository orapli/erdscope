from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Author(TimeStamped):
    name = models.CharField(max_length=100)
    email = models.EmailField(null=True)


class Post(TimeStamped):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name='posts')
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True)
    tags = models.ManyToManyField('Tag', through='PostTag')

    class Meta:
        db_table = 'blog_entries'


class Tag(models.Model):
    label = models.SlugField()


class PostTag(models.Model):
    post = models.ForeignKey('Post', on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)


class NotAModel:
    pass
