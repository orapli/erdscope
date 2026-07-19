<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Post extends Model
{
    public function user()
    {
        // no explicit foreign key given: must backfill the convention
        // default (user_id), not leave it unset.
        // return $this->belongsTo(Tag::class); <- commented-out, must be
        // ignored (comment-stripping) rather than misread as the real call
        return $this->belongsTo(User::class);
    }

    public function author()
    {
        // explicit foreign key override must win over the convention guess
        return $this->belongsTo(User::class, 'created_by_id');
    }

    public function parent()
    {
        return $this->belongsTo(Post::class, 'parent_id');
    }

    public function replies()
    {
        return $this->hasMany(Post::class, 'parent_id');
    }

    public function tags()
    {
        return $this->belongsToMany(Tag::class, 'posts_tags_pivot');
    }

    public function comments()
    {
        return $this->morphMany(Comment::class, 'commentable');
    }
}
