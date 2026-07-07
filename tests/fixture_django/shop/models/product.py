from django.db import models


class Product(models.Model):
    sku = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=100, db_column='product_name')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    owner = models.OneToOneField('blog.Author', on_delete=models.CASCADE, null=True)
